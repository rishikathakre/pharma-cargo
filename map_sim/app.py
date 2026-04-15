from __future__ import annotations

import csv
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents.cascade_orchestrator import CascadeOrchestrator
from agents.anomaly_agent import Anomaly
from agents.risk_agent import RecommendedAction, RiskAssessment, RiskLevel
from compliance.audit_logger import AuditLogger
from hitl.approval_queue import ApprovalQueue, ApprovalRequest, ApprovalStatus


ROOT = Path(__file__).resolve().parents[1]
DATA_RAW = ROOT / "data" / "raw"

# Prefer pharma routes if present; otherwise fall back to shipment.csv.
PHARMA_ROUTES_CSV = DATA_RAW / "pharma_routes.csv"
SHIPMENT_CSV = DATA_RAW / "shipment.csv"

# Airports dataset (CSV).
# Expected schema (OurAirports style): includes columns `name`, `latitude_deg`, `longitude_deg`.
US_AIRPORTS_CSV = DATA_RAW / "us-airports.csv"

# Small, editable coordinate lookup for demos.
# NOTE: The dropdown is driven by `us-airports.csv` only (per requirement).
COORDS: Dict[str, Tuple[float, float]] = {
    # Airports / cities (demo-friendly)
    "JFK Airport": (40.6413, -73.7781),
    "Heathrow Airport": (51.4700, -0.4543),
    "Frankfurt Airport": (50.0379, 8.5622),
    "Mumbai Airport": (19.0896, 72.8656),
    "O'Hare Airport": (41.9742, -87.9073),
    "Pudong Airport": (31.1443, 121.8083),
    "Singapore Changi Airport": (1.3644, 103.9915),
}


def _load_airports(path: Path) -> tuple[Dict[str, Tuple[float, float]], List[str]]:
    """
    Load airport name -> (lat, lon) from us-airports.csv.
    Dropdown options will use ONLY the `name` column values from this file.
    """
    if not path.exists():
        return {}, []

    out: Dict[str, Tuple[float, float]] = {}
    names: List[str] = []
    try:
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                name = (r.get("name") or "").strip()
                lat = r.get("latitude_deg")
                lon = r.get("longitude_deg")
                if not name or not lat or not lon:
                    continue
                try:
                    lat_f = float(lat)
                    lon_f = float(lon)
                except ValueError:
                    continue
                out.setdefault(name, (lat_f, lon_f))
                names.append(name)
    except Exception:
        return {}, []

    # De-duplicate but keep CSV order stable.
    seen = set()
    uniq: List[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return out, uniq


AIRPORT_COORDS, AIRPORT_NAMES = _load_airports(US_AIRPORTS_CSV)

# ---------------------------------------------------------------------------
# Dummy weather zones (demo-only)
# ---------------------------------------------------------------------------

WEATHER_ZONES = [
    {
        "code": "CALM",
        "label": "Calm weather",
        "color": "#22c55e",
        "severity": 0.10,
    },
    {
        "code": "WIND",
        "label": "High winds",
        "color": "#f59e0b",
        "severity": 0.55,
    },
    {
        "code": "STORM",
        "label": "Severe storm",
        "color": "#ef4444",
        "severity": 0.85,
    },
    {
        "code": "FOG",
        "label": "Low visibility (fog)",
        "color": "#94a3b8",
        "severity": 0.40,
    },
    {
        "code": "HEAT",
        "label": "Extreme heat",
        "color": "#a855f7",
        "severity": 0.70,
    },
]

# Quick lookup by code for overrides.
WEATHER_BY_CODE = {z["code"]: z for z in WEATHER_ZONES}

# How often dummy weather changes (wall-clock seconds).
WEATHER_REFRESH_SEC = 6.0

# Pipeline tick throttling.
# The UI polls fast (sub-second) to animate the map; the agent pipeline + audit writes
# should not run on every poll or the audit file will explode.
PIPELINE_TICK_MIN_INTERVAL_SEC = 2.0

# Auto-prune ARRIVED simulations so the sidebar doesn't grow forever.
SIM_AUTO_PRUNE_ARRIVED_AFTER_SEC = 60.0

# Thresholds for flight behavior (0..1 severity)
TAKEOFF_BLOCK_SEVERITY = 0.80   # origin weather this bad => no takeoff
LANDING_BLOCK_SEVERITY = 0.65   # destination weather this bad => holding pattern

# Destination "approach" radius where landing weather matters (km).
# Outside this, enroute cruise is not affected by weather.
APPROACH_RADIUS_KM = 180.0

# Consider the flight "arrived" once within this distance and landing weather is OK (km).
ARRIVAL_RADIUS_KM = 15.0


def _pick_weather(rng: random.Random) -> dict:
    z = rng.choice(WEATHER_ZONES)
    return {
        "code": z["code"],
        "label": z["label"],
        "color": z["color"],
        "severity": float(z["severity"]),
        # purely decorative for map visuals
        # Keep these modest so the circles don't dominate the map when zoomed in.
        "radius_km": float(rng.choice([12, 18, 25, 35, 45])),
    }

def _weather_from_code(code: str, rng: random.Random) -> dict:
    z = WEATHER_BY_CODE.get(code)
    if not z:
        return _pick_weather(rng)
    return {
        "code": z["code"],
        "label": z["label"],
        "color": z["color"],
        "severity": float(z["severity"]),
        "radius_km": float(rng.choice([12, 18, 25, 35, 45])),
    }


def _weather_for(seed: int, bucket: int, which: str) -> dict:
    """
    Deterministic weather generator per time bucket and location role.
    `which` should be "origin" or "destination".
    """
    salt = 1009 if which == "origin" else 2003
    rng = random.Random(seed + salt + (bucket * 7919))
    return _pick_weather(rng)


def _weather_for_override(seed: int, bucket: int, which: str, override_code: Optional[str]) -> dict:
    if not override_code or override_code.upper() == "RANDOM":
        return _weather_for(seed, bucket, which)
    # When overridden, keep the weather TYPE fixed (non-random) for demo control.
    # We intentionally do NOT vary by time bucket.
    salt = 3001 if which == "origin" else 4001
    rng = random.Random(seed + salt)
    return _weather_from_code(override_code.upper(), rng)


def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _interp_latlon(a: Tuple[float, float], b: Tuple[float, float], t: float) -> Tuple[float, float]:
    """
    Great-circle interpolation (slerp) for realistic flight paths.
    Returns intermediate point between a and b on the sphere.
    """
    t = max(0.0, min(1.0, t))

    lat1 = math.radians(a[0])
    lon1 = math.radians(a[1])
    lat2 = math.radians(b[0])
    lon2 = math.radians(b[1])

    def _to_vec(lat: float, lon: float) -> tuple[float, float, float]:
        return (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))

    def _dot(u: tuple[float, float, float], v: tuple[float, float, float]) -> float:
        return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]

    def _scale(u: tuple[float, float, float], s: float) -> tuple[float, float, float]:
        return (u[0] * s, u[1] * s, u[2] * s)

    def _add(u: tuple[float, float, float], v: tuple[float, float, float]) -> tuple[float, float, float]:
        return (u[0] + v[0], u[1] + v[1], u[2] + v[2])

    v1 = _to_vec(lat1, lon1)
    v2 = _to_vec(lat2, lon2)

    omega = math.acos(max(-1.0, min(1.0, _dot(v1, v2))))
    if not math.isfinite(omega) or omega < 1e-10:
        return (_lerp(a[0], b[0], t), _lerp(a[1], b[1], t))

    so = math.sin(omega)
    k1 = math.sin((1.0 - t) * omega) / so
    k2 = math.sin(t * omega) / so
    v = _add(_scale(v1, k1), _scale(v2, k2))

    x, y, z = v
    lat = math.atan2(z, math.sqrt(x * x + y * y))
    lon = math.atan2(y, x)
    return (math.degrees(lat), math.degrees(lon))


def _route_points(a: Tuple[float, float], b: Tuple[float, float], segments: int = 64) -> List[Tuple[float, float]]:
    """
    Canonical route polyline used for both drawing and movement.

    We start from a great-circle path (realistic), then add a small perpendicular
    bulge so the curve is visibly apparent even for shorter hops / zoomed-in views.
    """
    segments = max(16, int(segments))
    base = [_interp_latlon(a, b, i / segments) for i in range(segments + 1)]

    # Add a gentle bulge (in degrees) perpendicular to the chord in lat/lon space.
    # This is *visual polish* for the demo, not geodesy.
    dist_km = _haversine_km(a, b)
    bulge_deg = min(6.0, max(0.25, dist_km / 1800.0))  # longer routes curve more; cap it

    mid_i = len(base) // 2
    lat_a, lon_a = base[0]
    lat_b, lon_b = base[-1]
    dx = lon_b - lon_a
    dy = lat_b - lat_a
    norm = math.hypot(dx, dy) or 1.0
    # Perpendicular unit vector
    px = -dy / norm
    py = dx / norm
    # Stable direction so bulge doesn't flip randomly
    sign = 1.0 if (lat_a + lat_b) >= 0 else -1.0

    out: List[Tuple[float, float]] = []
    for i, (lat, lon) in enumerate(base):
        t = i / (len(base) - 1)
        w = math.sin(math.pi * t)  # 0 at ends, 1 at middle
        out.append((lat + (py * bulge_deg * w * sign), lon + (px * bulge_deg * w * sign)))
    return out


def _ease_in_out(t: float) -> float:
    """
    Smooth ease-in/ease-out for nicer motion.
    Uses smoothstep: 3t^2 - 2t^3 (monotonic, 0->1, zero slope at ends).
    """
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _load_routes() -> List[dict]:
    path = PHARMA_ROUTES_CSV if PHARMA_ROUTES_CSV.exists() else SHIPMENT_CSV
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    routes: List[dict] = []
    for r in rows:
        origin = (r.get("origin") or "").strip()
        destination = (r.get("destination") or "").strip()
        if not origin or not destination:
            continue
        routes.append(
            {
                "origin": origin,
                "destination": destination,
                "customs_clearance_time_days": float(r.get("customs_clearance_time_days") or 0),
                "delivery_status": (r.get("delivery_status") or "").strip(),
                "product_category": (r.get("product_category") or "").strip(),
                "type": (r.get("type") or "").strip(),
            }
        )
    return routes


ROUTES_CACHE = _load_routes()


class RouteOptions(BaseModel):
    origins: List[str]
    destinations: List[str]
    coords_known: List[str] = Field(default_factory=list)
    airports_indexed: int = 0
    routes_count: int
    source_file: str


class CreateSimRequest(BaseModel):
    origin: str
    destination: str
    # If a location name isn't in COORDS, client may supply coordinates.
    origin_lat: Optional[float] = None
    origin_lon: Optional[float] = None
    destination_lat: Optional[float] = None
    destination_lon: Optional[float] = None
    # Presentation speed: higher = faster movement.
    speed_multiplier: float = 10.0
    # How long the full trip should take on screen (seconds).
    duration_seconds: float = 45.0
    shipment_id: Optional[str] = None
    # Weather is dummy/demo-only; seed lets you make a run repeatable.
    weather_seed: Optional[int] = None
    origin_weather: Optional[str] = None   # e.g. "CALM" | "STORM" | "RANDOM"
    destination_weather: Optional[str] = None


class CreateSimResponse(BaseModel):
    sim_id: str
    shipment_id: str
    origin: str
    destination: str
    origin_lat: float
    origin_lon: float
    destination_lat: float
    destination_lon: float
    duration_seconds: float
    speed_multiplier: float
    weather_origin: dict
    weather_destination: dict
    origin_weather_override: Optional[str] = None
    destination_weather_override: Optional[str] = None
    route_points: Optional[List[List[float]]] = None


@dataclass
class Simulation:
    sim_id: str
    shipment_id: str
    origin_name: str
    destination_name: str
    origin: Tuple[float, float]
    destination: Tuple[float, float]
    created_at_monotonic: float
    duration_seconds: float
    speed_multiplier: float
    weather_seed: int
    origin_weather_override: Optional[str] = None
    destination_weather_override: Optional[str] = None
    route_points: List[Tuple[float, float]] = field(default_factory=list)

    # runtime state
    phase: str = "WAIT_TAKEOFF"  # WAIT_TAKEOFF | ENROUTE | HOLDING | ARRIVED
    enroute_start_monotonic: Optional[float] = None
    arrived_at_monotonic: Optional[float] = None
    _last_assessment: Optional[RiskAssessment] = None
    _last_anomalies: List[Anomaly] = field(default_factory=list)
    _last_actions: List[dict] = field(default_factory=list)
    _last_hitl_request_id: Optional[str] = None
    _last_tick_at_monotonic: Optional[float] = None
    _hitl_request_created_id: Optional[str] = None

    def _bucket(self, now: float) -> int:
        return int(max(0.0, now - self.created_at_monotonic) / WEATHER_REFRESH_SEC)

    def current_weather(self, now: float) -> tuple[dict, dict]:
        b = self._bucket(now)
        return (
            _weather_for_override(self.weather_seed, b, "origin", self.origin_weather_override),
            _weather_for_override(self.weather_seed, b, "destination", self.destination_weather_override),
        )

    def update_phase(self, now: float) -> None:
        w_origin, w_dest = self.current_weather(now)

        # 1) Takeoff gate: only origin weather matters, and only before takeoff.
        if self.phase == "WAIT_TAKEOFF":
            if w_origin["severity"] < TAKEOFF_BLOCK_SEVERITY:
                self.phase = "ENROUTE"
                self.enroute_start_monotonic = now
            return

        # 2) Enroute cruise: weather does NOT affect the flight until it reaches approach.
        if self.phase == "ENROUTE":
            # Compute current enroute position and distance remaining.
            pos = _interp_latlon(self.origin, self.destination, self.progress(now))
            dist_to_dest = _haversine_km(pos, self.destination)

            # If we're close enough to destination, destination weather can block landing.
            if dist_to_dest <= APPROACH_RADIUS_KM and w_dest["severity"] >= LANDING_BLOCK_SEVERITY:
                self.phase = "HOLDING"
                return

            # If we're basically at destination and weather is OK, arrive.
            if dist_to_dest <= ARRIVAL_RADIUS_KM and w_dest["severity"] < LANDING_BLOCK_SEVERITY:
                self.phase = "ARRIVED"
                self.arrived_at_monotonic = now
                return

            # Fallback: if progress hits 100%, attempt arrival/holding (covers short hops).
            if self.progress(now) >= 1.0:
                if w_dest["severity"] >= LANDING_BLOCK_SEVERITY:
                    self.phase = "HOLDING"
                else:
                    self.phase = "ARRIVED"
                    self.arrived_at_monotonic = now
            return

        # 3) Holding pattern: only destination weather matters.
        if self.phase == "HOLDING":
            if w_dest["severity"] < LANDING_BLOCK_SEVERITY:
                self.phase = "ARRIVED"
                self.arrived_at_monotonic = now
            return

    def progress(self, now: float) -> float:
        if self.phase == "ARRIVED":
            return 1.0
        if self.phase in ("WAIT_TAKEOFF", "HOLDING"):
            # not making route progress
            return 0.0 if self.phase == "WAIT_TAKEOFF" else 1.0
        # ENROUTE
        start = self.enroute_start_monotonic or now
        elapsed = (now - start) * self.speed_multiplier
        if self.duration_seconds <= 0:
            return 1.0
        linear = max(0.0, min(1.0, elapsed / self.duration_seconds))
        return _ease_in_out(linear)

    def current_position(self, now: float) -> Tuple[float, float]:
        self.update_phase(now)
        if self.phase == "WAIT_TAKEOFF":
            return self.origin
        if self.phase == "ENROUTE":
            # Follow the precomputed route points exactly so marker stays on the drawn line.
            pts = self.route_points or [self.origin, self.destination]
            p = max(0.0, min(1.0, self.progress(now)))
            if len(pts) <= 2:
                return _interp_latlon(self.origin, self.destination, p)
            idx_f = p * (len(pts) - 1)
            i = int(math.floor(idx_f))
            j = min(len(pts) - 1, i + 1)
            local_t = idx_f - i
            a = pts[i]
            b = pts[j]
            return (_lerp(a[0], b[0], local_t), _lerp(a[1], b[1], local_t))
        if self.phase == "HOLDING":
            # Simple holding pattern: circle around destination.
            lat0, lon0 = self.destination
            # radius ~ 0.12 degrees (~13km at equator) – purely visual
            radius_deg = 0.12
            angle = (now - self.created_at_monotonic) * 1.4
            return (lat0 + math.sin(angle) * radius_deg, lon0 + math.cos(angle) * radius_deg)
        # ARRIVED
        return self.destination

    def to_public(self) -> dict:
        now = time.monotonic()
        w_origin, w_dest = self.current_weather(now)
        lat, lon = self.current_position(now)
        dist_to_dest = _haversine_km((lat, lon), self.destination)
        out = {
            "sim_id": self.sim_id,
            "shipment_id": self.shipment_id,
            "origin": self.origin_name,
            "destination": self.destination_name,
            "phase": self.phase,
            "progress": round(self.progress(now), 4),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "distance_km": round(_haversine_km(self.origin, self.destination), 1),
            "distance_to_destination_km": round(dist_to_dest, 1),
            "weather_origin": w_origin,
            "weather_destination": w_dest,
            "route_points": [[round(p[0], 6), round(p[1], 6)] for p in (self.route_points or [])],
        }
        # Pipeline-enriched fields (added by the API handler tick runner).
        if self._last_assessment is not None:
            out["risk_level"] = self._last_assessment.risk_level.value
            out["risk_score"] = round(float(self._last_assessment.risk_score), 4)
        else:
            out["risk_level"] = None
            out["risk_score"] = None
        out["anomalies"] = [a.anomaly_type.value for a in (self._last_anomalies or [])]
        out["hitl_pending"] = self._last_hitl_request_id
        out["action_results"] = self._last_actions
        return out

    def generate_telemetry(self) -> dict:
        """
        Derive a telemetry payload from the current sim state.
        This intentionally mirrors the fields used by the agent pipeline.
        """
        now = time.monotonic()
        w_origin, w_dest = self.current_weather(now)
        lat, lon = self.current_position(now)

        # Weather severity drives temperature excursion and delay.
        temp = 5.0  # baseline safe
        if float(w_dest.get("severity", 0.0)) > 0.65:
            temp = 5.0 + (float(w_dest["severity"]) - 0.65) * 35.0

        delay_hours = 0.0
        if self.phase == "HOLDING":
            delay_hours = round(float(w_dest.get("severity", 0.0)) * 12.0, 1)

        return {
            "shipment_id": self.shipment_id,
            "container_id": f"CNT-{self.sim_id[:8]}",
            "timestamp": time.time(),
            "temperature_c": round(float(temp), 2),
            "humidity_pct": round(50.0 + float(w_dest.get("severity", 0.0)) * 30.0, 1),
            "shock_g": round(float(w_dest.get("severity", 0.0)) * 2.5, 2),
            "latitude": round(float(lat), 6),
            "longitude": round(float(lon), 6),
            "altitude_m": 10000.0 if self.phase == "ENROUTE" else 0.0,
            "customs_status": "HOLD" if self.phase == "HOLDING" else "CLEARED",
            "flight_status": (
                "DELAYED" if self.phase == "HOLDING"
                else ("ON_TIME" if self.phase == "ENROUTE" else "DIVERTED" if self.phase == "WAIT_TAKEOFF" else "ARRIVED")
            ),
            "delay_hours": delay_hours,
            "battery_pct": 95.0,
            "carrier": "DHL Lifesciences",
            "origin": self.origin_name,
            "destination": self.destination_name,
        }


SIMS: Dict[str, Simulation] = {}

_approval_queue = ApprovalQueue()
_orchestrator = CascadeOrchestrator(approval_queue=_approval_queue)
_audit = AuditLogger()

def _pending_request_for_shipment(shipment_id: str) -> Optional[ApprovalRequest]:
    for r in _approval_queue.pending():
        if r.shipment_id == shipment_id:
            return r
    return None

def _tick_pipeline(sim: Simulation) -> None:
    """
    Run one non-blocking pipeline tick for a simulation.
    NOTE: We intentionally DO NOT block on HITL decisions here.
    """
    now = time.monotonic()
    last = sim._last_tick_at_monotonic
    if last is not None and (now - last) < PIPELINE_TICK_MIN_INTERVAL_SEC:
        return
    telemetry = sim.generate_telemetry()
    record = _orchestrator.tel.ingest(telemetry)
    anomalies = _orchestrator.ano.analyse(record)
    assessment = _orchestrator.risk.assess(record, anomalies)
    assessment.metadata.update(
        {
            "carrier": telemetry.get("carrier", "Unknown"),
            "origin": telemetry.get("origin", "Unknown"),
            "destination": telemetry.get("destination", "Unknown"),
            "product_id": telemetry.get("product_id"),
        }
    )

    sim._last_anomalies = anomalies
    sim._last_assessment = assessment

    # Audit: anomalies + assessment each tick (demo visibility).
    for a in anomalies:
        _audit.log_anomaly(a)
    _audit.log_assessment(assessment)

    # Compliance check (no blocking).
    try:
        compliant, violations = _orchestrator.gdp.validate(record, assessment)
        if violations:
            for v in violations:
                _audit.log_compliance_violation(record.shipment_id, v)
        assessment.metadata["gdp_compliant"] = bool(compliant)
    except Exception as e:
        assessment.metadata["gdp_check_error"] = str(e)

    # HITL: create at most ONE request per shipment until risk resets to LOW.
    # This avoids duplicate alert cards when the shipment remains high risk across ticks.
    sim._last_hitl_request_id = None
    if assessment.risk_level == RiskLevel.LOW:
        sim._hitl_request_created_id = None
    else:
        # If there's a currently pending request, show it.
        existing = _pending_request_for_shipment(assessment.shipment_id)
        if existing is not None:
            sim._last_hitl_request_id = existing.request_id
            sim._hitl_request_created_id = existing.request_id
        else:
            # No pending request. Only create a new one if we never created one
            # for this shipment since the last reset-to-LOW.
            if sim._hitl_request_created_id is None:
                req = _approval_queue.submit(assessment)
                sim._last_hitl_request_id = req.request_id
                sim._hitl_request_created_id = req.request_id

    sim._last_tick_at_monotonic = now


app = FastAPI(title="Map Simulation (Isolated)", version="0.1.0")

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (static_dir / "index.html").read_text(encoding="utf-8")


@app.get("/api/options", response_model=RouteOptions)
def options() -> RouteOptions:
    # IMPORTANT: Dropdowns are intentionally driven by us-airports.csv `name` only.
    # Large <select> lists can be very slow to render in the browser.
    # Limit options to keep the UI responsive (users can still type-to-jump within the list).
    max_items = 600
    origins = AIRPORT_NAMES[:max_items]
    destinations = AIRPORT_NAMES[:max_items]
    # Never expose absolute local paths in the UI.
    source = (PHARMA_ROUTES_CSV if PHARMA_ROUTES_CSV.exists() else SHIPMENT_CSV).name
    return RouteOptions(
        origins=origins,
        destinations=destinations,
        coords_known=sorted(COORDS.keys()),
        airports_indexed=len(AIRPORT_COORDS),
        routes_count=len(ROUTES_CACHE),
        source_file=source,
    )


@app.get("/api/lookup")
def lookup(name: str) -> dict:
    """
    Resolve a location name to coordinates.

    Sources (in order):
      1) Built-in COORDS (small curated demo set)
      2) AIRPORT_COORDS (from data/raw/us-airports.csv)
    """
    key = (name or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="Missing name")
    if key in COORDS:
        lat, lon = COORDS[key]
        return {"found": True, "name": key, "lat": lat, "lon": lon, "source": "builtin"}
    if key in AIRPORT_COORDS:
        lat, lon = AIRPORT_COORDS[key]
        return {"found": True, "name": key, "lat": lat, "lon": lon, "source": "us_airports_csv"}
    return {
        "found": False,
        "name": key,
        "detail": (
            "Not found. Provide lat/lon manually, add to COORDS in map_sim/app.py, "
            "or update data/raw/us-airports.csv."
        ),
    }


def _resolve_point(name: str, lat: Optional[float], lon: Optional[float]) -> Tuple[float, float]:
    if name in COORDS:
        return COORDS[name]
    if name in AIRPORT_COORDS:
        return AIRPORT_COORDS[name]
    if lat is not None and lon is not None:
        return (float(lat), float(lon))
    raise HTTPException(
        status_code=422,
        detail=(
            f"Unknown location '{name}'. Provide origin_lat/origin_lon or "
            "add it to COORDS in map_sim/app.py, or add it to data/raw/us-airports.csv."
        ),
    )


@app.post("/api/sim", response_model=CreateSimResponse)
def create_sim(req: CreateSimRequest) -> CreateSimResponse:
    sim_id = str(uuid.uuid4())
    shipment_id = req.shipment_id or f"SHP-MAP-{sim_id[:8]}"

    origin = _resolve_point(req.origin, req.origin_lat, req.origin_lon)
    destination = _resolve_point(req.destination, req.destination_lat, req.destination_lon)

    # Deterministic per-sim weather if seed provided; otherwise random.
    seed = int(req.weather_seed) if req.weather_seed is not None else int(uuid.uuid4().int % 1_000_000_000)
    now = time.monotonic()
    b = int(max(0.0, now - now) / WEATHER_REFRESH_SEC)  # always 0 at creation
    w_origin = _weather_for_override(seed, b, "origin", req.origin_weather)
    w_dest = _weather_for_override(seed, b, "destination", req.destination_weather)

    sim = Simulation(
        sim_id=sim_id,
        shipment_id=shipment_id,
        origin_name=req.origin,
        destination_name=req.destination,
        origin=origin,
        destination=destination,
        created_at_monotonic=time.monotonic(),
        duration_seconds=float(req.duration_seconds),
        speed_multiplier=float(req.speed_multiplier),
        weather_seed=seed,
        origin_weather_override=req.origin_weather,
        destination_weather_override=req.destination_weather,
        route_points=_route_points(origin, destination, segments=72),
    )
    SIMS[sim_id] = sim

    return CreateSimResponse(
        sim_id=sim_id,
        shipment_id=shipment_id,
        origin=req.origin,
        destination=req.destination,
        origin_lat=origin[0],
        origin_lon=origin[1],
        destination_lat=destination[0],
        destination_lon=destination[1],
        duration_seconds=sim.duration_seconds,
        speed_multiplier=sim.speed_multiplier,
        weather_origin=w_origin,
        weather_destination=w_dest,
        origin_weather_override=req.origin_weather,
        destination_weather_override=req.destination_weather,
        route_points=[[round(p[0], 6), round(p[1], 6)] for p in sim.route_points],
    )


class WeatherControlRequest(BaseModel):
    origin_weather: Optional[str] = None
    destination_weather: Optional[str] = None


@app.post("/api/sim/{sim_id}/weather")
def set_weather(sim_id: str, body: WeatherControlRequest) -> dict:
    sim = SIMS.get(sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation not found")
    if body.origin_weather is not None:
        sim.origin_weather_override = body.origin_weather
    if body.destination_weather is not None:
        sim.destination_weather_override = body.destination_weather
    return {
        "sim_id": sim_id,
        "origin_weather_override": sim.origin_weather_override,
        "destination_weather_override": sim.destination_weather_override,
    }


@app.get("/api/sim/{sim_id}")
def get_sim(sim_id: str) -> dict:
    sim = SIMS.get(sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation not found")
    _tick_pipeline(sim)
    return sim.to_public()


@app.get("/api/sims")
def list_sims() -> List[dict]:
    out: List[dict] = []
    now = time.monotonic()
    to_delete: List[str] = []
    for sid, s in SIMS.items():
        if s.phase == "ARRIVED" and s.arrived_at_monotonic is not None:
            if (now - s.arrived_at_monotonic) >= SIM_AUTO_PRUNE_ARRIVED_AFTER_SEC:
                to_delete.append(sid)
                continue
        _tick_pipeline(s)
        out.append(s.to_public())
    for sid in to_delete:
        SIMS.pop(sid, None)
    return out


@app.get("/api/audit/info")
def audit_info() -> dict:
    return _audit.file_info()


@app.post("/api/audit/clear")
def audit_clear() -> dict:
    """
    Demo-only endpoint to truncate the audit log file.
    Enable by setting AUDIT_ALLOW_TRUNCATE=1 in the environment.
    """
    try:
        _audit.truncate()
        return {"ok": True, "audit": _audit.file_info()}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/api/demo/reset")
def demo_reset() -> dict:
    """
    Demo reset: clear in-memory sims and pending HITL requests.
    (Does NOT delete audit.jsonl; use /api/audit/clear for that when enabled.)
    """
    sims_before = len(SIMS)
    SIMS.clear()
    pending_cleared = _approval_queue.clear(pending_only=True)
    return {"ok": True, "sims_cleared": sims_before, "hitl_pending_cleared": pending_cleared}


class ApproveRequestBody(BaseModel):
    operator: str = "map_operator"
    approved_actions: Optional[List[str]] = None
    notes: str = ""


class RejectRequestBody(BaseModel):
    operator: str = "map_operator"
    notes: str = ""


@app.get("/api/hitl/pending")
def pending_hitl() -> List[dict]:
    return [r.to_dict() for r in _approval_queue.pending()]


@app.post("/api/hitl/{request_id}/approve")
def approve_hitl(request_id: str, body: ApproveRequestBody) -> dict:
    req = _approval_queue.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")

    actions: Optional[List[RecommendedAction]] = None
    if body.approved_actions is not None:
        try:
            actions = [RecommendedAction(a) for a in body.approved_actions]
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid approved_actions")

    updated = _approval_queue.approve(request_id, body.operator, actions, body.notes)
    _audit.log_hitl_decision(updated)

    # Execute approved actions using the latest assessment we have for this shipment.
    sim = next((s for s in SIMS.values() if s.shipment_id == updated.shipment_id), None)
    assessment = sim._last_assessment if sim else None
    if assessment is not None and updated.approved_actions:
        results = _orchestrator.act.execute(assessment, updated.approved_actions)
        sim._last_actions = [r.to_dict() for r in results] if sim else []
        for r in results:
            _audit.log_action_result(r)

    # Clear pending id from the sim cache if this was the active pending.
    if sim and sim._last_hitl_request_id == request_id:
        sim._last_hitl_request_id = None
    return updated.to_dict()


@app.post("/api/hitl/{request_id}/reject")
def reject_hitl(request_id: str, body: RejectRequestBody) -> dict:
    req = _approval_queue.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    updated = _approval_queue.reject(request_id, body.operator, body.notes)
    _audit.log_hitl_decision(updated)
    sim = next((s for s in SIMS.values() if s.shipment_id == updated.shipment_id), None)
    if sim and sim._last_hitl_request_id == request_id:
        sim._last_hitl_request_id = None
    return updated.to_dict()

