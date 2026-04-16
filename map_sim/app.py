from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is importable when running from subdir (e.g., `python map_sim/app.py`).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import csv
import math
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents.cascade_orchestrator import CascadeOrchestrator
from agents.anomaly_agent import Anomaly
from agents.risk_agent import RecommendedAction, RiskAssessment, RiskLevel
from compliance.audit_logger import AuditLogger
from hitl.approval_queue import ApprovalQueue, ApprovalRequest, ApprovalStatus


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
# Cold-storage facility coordinates (keyed by IATA code).
# Mirrors the keys in reroute_engine._COLD_STORAGE_FACILITIES.
# Used to animate the flight path change after a reroute is approved.
# ---------------------------------------------------------------------------
COLD_STORAGE_COORDS: Dict[str, Tuple[float, float]] = {
    "JFK": (40.6413, -73.7781),
    "LAX": (33.9425, -118.4081),
    "ORD": (41.9742, -87.9073),
    "MIA": (25.7959, -80.2870),
    "BOS": (42.3656, -71.0096),
    "FRA": (50.0379,   8.5622),
    "AMS": (52.3086,   4.7639),
    "CDG": (49.0097,   2.5479),
    "LHR": (51.4700,  -0.4543),
    "ZRH": (47.4647,   8.5492),
    "SIN": ( 1.3644, 103.9915),
    "HKG": (22.3080, 113.9185),
    "PVG": (31.1443, 121.8083),
    "BOM": (19.0896,  72.8656),
    "NRT": (35.7647, 140.3864),
}

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

# ---------------------------------------------------------------------------
# Congestion / queueing (demo-only)
# ---------------------------------------------------------------------------
#
# We simulate airport congestion as a per-origin runway queue:
# - Only one simulation per origin is allowed to "take off" per service interval.
# - Congestion level increases the service interval (slower departures).
# - Priority shipments can jump ahead in the queue (time-sensitive pharma cargo).
#
# This is intentionally simple, deterministic, and cheap to compute.
#
CONGESTION_REFRESH_SEC = 10.0  # how often congestion may change (if randomized)
DEFAULT_CONGESTION_LEVEL = 2   # 0 (none) .. 5 (heavy)

# One departure per origin every this many seconds at congestion_level=0
RUNWAY_BASE_SERVICE_INTERVAL_SEC = 2.0
# Additional seconds per congestion level (so level 5 feels meaningfully slower)
RUNWAY_SERVICE_INTERVAL_PER_LEVEL_SEC = 2.0

# Destination "approach" radius where landing weather matters (km).
# Outside this, enroute cruise is not affected by weather.
APPROACH_RADIUS_KM = 180.0

# Consider the flight "arrived" once within this distance and landing weather is OK (km).
ARRIVAL_RADIUS_KM = 15.0

# Minimum wall-clock seconds a flight must spend in HOLDING before it can land.
# Prevents instant HOLDING → ARRIVED flips when weather clears on the next bucket.
# At typical speed_multiplier=60 this represents ~15 simulated minutes on screen.
MIN_HOLDING_SECONDS = 15.0


def _clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(x)))


class RunwayQueueManager:
    """
    Per-origin runway queue with congestion + priority.

    - Each origin has a queue of sim_ids waiting for takeoff.
    - Only one sim per origin can depart per computed service interval.
    - Priority (higher int) can reorder the queue (front of line).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._queue_by_origin: Dict[str, List[str]] = {}
        self._last_departure_by_origin: Dict[str, float] = {}
        self._congestion_by_origin: Dict[str, int] = {}

    def set_congestion_level(self, origin: str, level: int) -> None:
        with self._lock:
            self._congestion_by_origin[origin] = _clamp_int(level, 0, 5)

    def get_congestion_level(self, origin: str) -> int:
        with self._lock:
            return self._congestion_by_origin.get(origin, DEFAULT_CONGESTION_LEVEL)

    def service_interval_sec(self, origin: str) -> float:
        level = self.get_congestion_level(origin)
        return RUNWAY_BASE_SERVICE_INTERVAL_SEC + (level * RUNWAY_SERVICE_INTERVAL_PER_LEVEL_SEC)

    def enqueue(self, origin: str, sim_id: str) -> None:
        with self._lock:
            q = self._queue_by_origin.setdefault(origin, [])
            if sim_id not in q:
                q.append(sim_id)

    def remove(self, origin: str, sim_id: str) -> None:
        with self._lock:
            q = self._queue_by_origin.get(origin)
            if not q:
                return
            try:
                q.remove(sim_id)
            except ValueError:
                pass

    def position(self, origin: str, sim_id: str) -> Optional[int]:
        with self._lock:
            q = self._queue_by_origin.get(origin, [])
            if sim_id not in q:
                return None
            return q.index(sim_id) + 1  # 1-based

    def reorder_by_priority(self, origin: str, sims: Dict[str, "Simulation"]) -> None:
        """
        Sort the queue by priority descending, then FIFO within equal priority.
        """
        with self._lock:
            q = self._queue_by_origin.get(origin, [])
            if len(q) <= 1:
                return
            # stable sort: Python sort is stable; we preserve order within same priority
            q.sort(key=lambda sid: getattr(sims.get(sid), "priority", 0), reverse=True)

    def can_takeoff(self, sim: "Simulation", now: float, origin_weather_ok: bool) -> bool:
        """
        Returns True when the simulation is cleared for takeoff from its origin queue.
        Weather is still checked by the caller; this function models congestion only.

        Single-plane rule: if this sim is the only one at its origin, skip the
        service-interval check — congestion only matters when planes are queuing.
        """
        origin = sim.origin_name
        sim_id = sim.sim_id
        with self._lock:
            q = self._queue_by_origin.setdefault(origin, [])
            if sim_id not in q:
                q.append(sim_id)

            # Must be at the front of the queue
            if not q or q[0] != sim_id:
                return False

            # If weather blocks takeoff, do not consume the runway slot.
            if not origin_weather_ok:
                return False

            # Only enforce service interval when there are multiple planes at this
            # origin — a lone plane waiting on an otherwise empty runway is instant.
            if len(q) > 1:
                last = self._last_departure_by_origin.get(origin, 0.0)
                if now - last < self.service_interval_sec(origin):
                    return False

            # Grant takeoff: consume slot and pop from queue
            self._last_departure_by_origin[origin] = now
            try:
                q.pop(0)
            except Exception:
                pass
            return True


RUNWAY = RunwayQueueManager()


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
    # Product being shipped — must match a product_id in product_catalogue.json.
    product_id: Optional[str] = None
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
    product_id: str = "VACC-STANDARD"
    origin_weather_override: Optional[str] = None
    destination_weather_override: Optional[str] = None
    route_points: List[Tuple[float, float]] = field(default_factory=list)

    # runtime state
    phase: str = "WAIT_TAKEOFF"  # WAIT_TAKEOFF | ENROUTE | HOLDING | ARRIVED
    enroute_start_monotonic: Optional[float] = None
    holding_start_monotonic: Optional[float] = None
    arrived_at_monotonic: Optional[float] = None
    _last_assessment: Optional[RiskAssessment] = None
    _last_anomalies: List[Anomaly] = field(default_factory=list)
    _last_actions: List[dict] = field(default_factory=list)
    _last_hitl_request_id: Optional[str] = None
    _last_tick_at_monotonic: Optional[float] = None
    _hitl_request_created_id: Optional[str] = None

    # congestion / priority (demo-only)
    # Higher priority moves a shipment ahead of others waiting to take off.
    # 0 = normal, 100 = emergency/critical.
    priority: int = 0
    priority_reason: str = ""

    # reroute state — set by apply_reroute() when HITL approves REROUTE_SHIPMENT
    rerouted: bool = False
    reroute_destination_name: str = ""
    reroute_plan: Optional[Dict[str, Any]] = None
    original_route_points: List[Tuple[float, float]] = field(default_factory=list)
    original_destination_name: str = ""
    original_destination: Optional[Tuple[float, float]] = None

    # pipeline (background thread) state
    _last_risk_state: Optional[PipelineState] = None
    _last_telemetry: Optional[Dict[str, Any]] = None
    _pipeline_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _bucket(self, now: float) -> int:
        return int(max(0.0, now - self.created_at_monotonic) / WEATHER_REFRESH_SEC)

    def current_weather(self, now: float) -> tuple[dict, dict]:
        b = self._bucket(now)
        return (
            _weather_for_override(self.weather_seed, b, "origin", self.origin_weather_override),
            _weather_for_override(self.weather_seed, b, "destination", self.destination_weather_override),
        )

    def wait_reason(self, now: float) -> str:
        """Human-readable reason why a WAIT_TAKEOFF sim has not departed yet."""
        if self.phase != "WAIT_TAKEOFF":
            return ""
        w_origin, _ = self.current_weather(now)
        if w_origin["severity"] >= TAKEOFF_BLOCK_SEVERITY:
            return f"Weather hold at origin: {w_origin['label']} (severity {w_origin['severity']:.2f})"
        pos = RUNWAY.position(self.origin_name, self.sim_id)
        if pos is not None and pos > 1:
            interval = RUNWAY.service_interval_sec(self.origin_name)
            return f"Runway queue position {pos} — congestion level {RUNWAY.get_congestion_level(self.origin_name)} ({interval:.0f}s between departures)"
        return "Awaiting runway clearance"

    def update_phase(self, now: float) -> None:
        w_origin, w_dest = self.current_weather(now)

        # 1) Takeoff gate: only origin weather matters, and only before takeoff.
        if self.phase == "WAIT_TAKEOFF":
            origin_weather_ok = w_origin["severity"] < TAKEOFF_BLOCK_SEVERITY
            # Congestion queue: must be cleared for takeoff from runway queue.
            # Priority shipments are reordered externally (see /api/sim/{id}/priority).
            cleared = RUNWAY.can_takeoff(self, now, origin_weather_ok)
            if cleared:
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
                self.holding_start_monotonic = now
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
                    self.holding_start_monotonic = now
                else:
                    self.phase = "ARRIVED"
                    self.arrived_at_monotonic = now
            return

        # 3) Holding pattern: only destination weather matters.
        # Also require MIN_HOLDING_SECONDS to have elapsed so the plane visibly
        # circles before landing — prevents instant HOLDING → ARRIVED flips when
        # weather clears on the very next weather bucket.
        if self.phase == "HOLDING":
            held_for = now - (self.holding_start_monotonic or now)
            # Scale minimum holding time by speed_multiplier so fast demos don't
            # still wait 15 real seconds; at speed 1 it's 15 s, at speed 60 it's 0.25 s.
            min_hold = MIN_HOLDING_SECONDS / max(1.0, float(self.speed_multiplier))
            if w_dest["severity"] < LANDING_BLOCK_SEVERITY and held_for >= min_hold:
                self.phase = "ARRIVED"
                self.arrived_at_monotonic = now
            return

    def progress(self, now: float) -> float:
        if self.phase == "ARRIVED":
            return 1.0
        if self.phase in ("WAIT_TAKEOFF", "HOLDING"):
            return 0.0 if self.phase == "WAIT_TAKEOFF" else 1.0
        # ENROUTE
        # duration_seconds = simulated flight time in seconds (e.g. 28800 for 8 h).
        # speed_multiplier = simulated seconds consumed per real wall-clock second
        # (e.g. 60 → 1 min of flight per second on screen, so 8 h completes in 8 min).
        # On-screen completion time = duration_seconds / speed_multiplier.
        start = self.enroute_start_monotonic or now
        elapsed = (now - start) * self.speed_multiplier   # simulated seconds elapsed
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
            # Scale angular velocity with speed_multiplier so circling "feels" consistent.
            angle = (now - self.created_at_monotonic) * 1.4 * max(1.0, float(self.speed_multiplier))
            return (lat0 + math.sin(angle) * radius_deg, lon0 + math.cos(angle) * radius_deg)
        # ARRIVED
        return self.destination

    def apply_reroute(self, plan_dict: Dict[str, Any]) -> bool:
        """
        Redirect this simulation to a new destination based on an approved reroute plan.

        For COLD_STORAGE plans: the flight curves toward the cold-storage hub (IATA-keyed).
        For LAST_MILE_COURIER / ORIGINAL_ROUTE: no visual path change (no coords available).

        Returns True if the visual reroute was applied, False if skipped.
        """
        chosen_path = plan_dict.get("chosen_path", "")
        if chosen_path == "ORIGINAL_ROUTE":
            self.rerouted = True
            self.reroute_plan = plan_dict
            return False

        iata = plan_dict.get("cold_storage_iata", "")
        new_coords: Optional[Tuple[float, float]] = None
        new_name = ""

        if chosen_path == "COLD_STORAGE" and iata and iata in COLD_STORAGE_COORDS:
            new_coords = COLD_STORAGE_COORDS[iata]
            new_name   = plan_dict.get("cold_storage_facility") or f"{iata} Cold-Storage Hub"
        elif chosen_path == "LAST_MILE_COURIER":
            # No specific hub coords: mark rerouted but keep original destination on map.
            self.rerouted      = True
            self.reroute_plan  = plan_dict
            self.reroute_destination_name = plan_dict.get("recommended_carrier", "Emergency Courier")
            return False

        if new_coords is None:
            return False

        now = time.monotonic()
        current_pos = self.current_position(now)

        # Save original route for faded display on the frontend
        self.original_route_points   = list(self.route_points)
        self.original_destination_name = self.destination_name
        self.original_destination    = self.destination

        # Build new route from current position to cold-storage hub
        # For short diversions, use a simple straight line (no arc bulge)
        dist_km = 0.0
        try:
            dlat = new_coords[0] - current_pos[0]
            dlon = new_coords[1] - current_pos[1]
            # Rough km estimate
            dist_km = ((dlat * 111.0) ** 2 + (dlon * 111.0 * math.cos(math.radians(current_pos[0]))) ** 2) ** 0.5
        except Exception:
            pass

        if dist_km < 500:
            # Short diversion — straight line, no arc
            n = max(4, int(dist_km / 10))
            new_route_pts = [
                (current_pos[0] + (new_coords[0] - current_pos[0]) * i / n,
                 current_pos[1] + (new_coords[1] - current_pos[1]) * i / n)
                for i in range(n + 1)
            ]
        else:
            new_route_pts = _route_points(current_pos, new_coords, segments=48)

        # Redirect the sim: update destination + route + reset progress clock
        self.destination      = new_coords
        self.destination_name = new_name
        self.route_points     = new_route_pts

        # ETA from the reroute plan → simulated flight seconds for the new leg.
        # Real on-screen time = eta_hours * 3600 / speed_multiplier.
        eta_hours = float(plan_dict.get("eta_hours", 2.0))
        self.duration_seconds        = eta_hours * 3600.0
        self.enroute_start_monotonic = now   # reset progress to 0

        # Ensure we're in ENROUTE (not HOLDING) so the new path plays out
        if self.phase in ("HOLDING", "ARRIVED"):
            self.phase = "ENROUTE"
            self.holding_start_monotonic = None

        self.rerouted               = True
        self.reroute_destination_name = new_name
        self.reroute_plan           = plan_dict

        import logging as _log
        _log.getLogger(__name__).info(
            "[%s] Reroute applied: %s → %s (iata=%s  eta=%.1fh)",
            self.shipment_id, self.original_destination_name, new_name, iata, eta_hours,
        )
        return True

    def to_public(self) -> dict:
        now = time.monotonic()
        w_origin, w_dest = self.current_weather(now)
        lat, lon = self.current_position(now)
        dist_to_dest = _haversine_km((lat, lon), self.destination)
        congestion_level = RUNWAY.get_congestion_level(self.origin_name)
        queue_position = RUNWAY.position(self.origin_name, self.sim_id) if self.phase == "WAIT_TAKEOFF" else None

        out = {
            "sim_id": self.sim_id,
            "shipment_id": self.shipment_id,
            "origin": self.origin_name,
            "destination": self.destination_name,
            "phase": self.phase,
            "congestion_level": congestion_level,
            "runway_service_interval_sec": RUNWAY.service_interval_sec(self.origin_name),
            "queue_position": queue_position,  # 1-based; only meaningful while WAIT_TAKEOFF
            "wait_reason": self.wait_reason(now),  # non-empty only while WAIT_TAKEOFF
            "priority": self.priority,
            "priority_reason": self.priority_reason,
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

        # Reroute visual state
        out["rerouted"] = self.rerouted
        if self.rerouted:
            out["reroute_destination_name"] = self.reroute_destination_name
            out["reroute_plan"]             = self.reroute_plan
            out["original_route_points"]    = [
                [round(p[0], 6), round(p[1], 6)] for p in (self.original_route_points or [])
            ]
            out["original_destination_name"] = self.original_destination_name
            if self.original_destination:
                out["original_destination_lat"] = round(self.original_destination[0], 6)
                out["original_destination_lon"] = round(self.original_destination[1], 6)
        return out

    def generate_telemetry(self) -> dict:
        """
        Generate realistic cold-chain telemetry keyed to current phase and destination weather severity.
        Severity is expected in [0.0, 1.0].
        """
        now_mono = time.monotonic()
        phase = self.phase

        _, w_dest = self.current_weather(now_mono)
        severity = float(w_dest.get("severity", 0.0) or 0.0)
        severity = max(0.0, min(1.0, severity))

        # Position for telemetry should match the current phase without forcing transitions.
        if phase == "WAIT_TAKEOFF":
            lat, lon = self.origin
        elif phase == "ENROUTE":
            lat, lon = _interp_latlon(self.origin, self.destination, self.progress(now_mono))
        elif phase == "HOLDING":
            lat0, lon0 = self.destination
            radius_deg = 0.12
            angle = (now_mono - self.created_at_monotonic) * 1.4 * max(1.0, float(self.speed_multiplier))
            lat, lon = (lat0 + math.sin(angle) * radius_deg, lon0 + math.cos(angle) * radius_deg)
        else:  # ARRIVED
            lat, lon = self.destination

        # Small deterministic jitter per sim + time slice so readings look "alive" but stable.
        jitter_bucket = int(now_mono * 2.0)  # changes ~2x/sec max
        rng = random.Random((hash(self.sim_id) ^ (jitter_bucket * 1_000_003)) & 0xFFFFFFFF)

        # Map severity -> temperature excursion bands.
        if severity < 0.5:
            temp_target = rng.uniform(4.0, 7.5)  # safe 2–8°C range
        elif severity < 0.7:
            t = (severity - 0.5) / 0.2
            temp_target = 9.0 + t * 3.0 + rng.uniform(-0.4, 0.4)
        else:
            t = (severity - 0.7) / 0.3
            temp_target = 13.0 + t * 5.0 + rng.uniform(-0.6, 0.6)
        temp_target = max(2.0, min(18.5, temp_target))

        delay_hours = 0.0
        flight_status = "ON_TIME"
        customs_status = "CLEARED"

        # --- Battery drain model ---
        # Battery powers the container's active cooling unit.
        # Drains linearly over the journey; extra drain during HOLDING (heavier
        # cooling load while circling) and a small jitter for realism.
        elapsed_sec = max(0.0, now_mono - self.created_at_monotonic)
        journey_fraction = elapsed_sec / max(1.0, float(self.duration_seconds))
        # Normal drain: 100% → ~65% over the full journey.
        battery_base = 100.0 - journey_fraction * 35.0
        # Extra drain if we have been in a holding pattern.
        holding_elapsed_sec = 0.0
        if self.holding_start_monotonic is not None:
            holding_elapsed_sec = max(0.0, now_mono - self.holding_start_monotonic)
        holding_fraction = holding_elapsed_sec / max(1.0, float(self.duration_seconds))
        # Holding draws an additional 30% worth of battery over the same journey window.
        battery_base -= holding_fraction * 30.0
        # Small deterministic jitter (±2%).
        battery_pct = round(max(0.0, min(100.0, battery_base + rng.uniform(-2.0, 2.0))), 1)

        if phase == "WAIT_TAKEOFF":
            temperature_c = rng.uniform(4.0, 6.0)
            humidity_pct = rng.uniform(40.0, 55.0)
            shock_g = rng.uniform(0.02, 0.08)
            altitude_m = rng.uniform(0.0, 30.0)
        elif phase == "ENROUTE":
            temperature_c = temp_target
            humidity_pct = rng.uniform(18.0, 35.0)
            shock_g = rng.uniform(0.01, 0.06)
            p = max(0.0, min(1.0, float(self.progress(now_mono))))
            ramp = 0.12
            climb = 1.0 if p >= ramp else (p / ramp)
            descend = 1.0 if p <= (1.0 - ramp) else ((1.0 - p) / ramp)
            alt_factor = max(0.0, min(1.0, min(climb, descend)))
            cruise_alt_m = 10600.0
            altitude_m = max(0.0, cruise_alt_m * alt_factor + rng.uniform(-120.0, 120.0))
            enroute_elapsed_h = max(0.0, (now_mono - (self.enroute_start_monotonic or now_mono)) / 3600.0)
            if severity > 0.6:
                delay_hours = enroute_elapsed_h * (severity - 0.6) * 6.0
            flight_status = "DELAYED" if delay_hours >= 0.5 else "ON_TIME"
        elif phase == "HOLDING":
            customs_status = "HOLD"
            flight_status = "DELAYED"
            holding_elapsed_h = holding_elapsed_sec / 3600.0
            delay_hours = max(1.0, holding_elapsed_h + severity * 3.0)
            temperature_c = min(18.0, temp_target + holding_elapsed_h * 4.0 + rng.uniform(-0.3, 0.3))
            humidity_pct = rng.uniform(30.0, 50.0)
            shock_g = rng.uniform(0.02, 0.09)
            osc = math.sin((now_mono - self.created_at_monotonic) * 0.35 * max(1.0, float(self.speed_multiplier)))
            altitude_m = max(0.0, 6100.0 + osc * 450.0 + rng.uniform(-80.0, 80.0))
        else:  # ARRIVED
            temperature_c = rng.uniform(4.0, 6.0)
            humidity_pct = rng.uniform(40.0, 60.0)
            shock_g = rng.uniform(0.01, 0.05)
            altitude_m = 0.0

        return {
            "shipment_id": self.shipment_id,
            "container_id": f"CNT-{self.sim_id[:8]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "temperature_c": round(float(temperature_c), 2),
            "humidity_pct": round(float(humidity_pct), 1),
            "shock_g": round(float(shock_g), 3),
            "battery_pct": battery_pct,
            "latitude": round(float(lat), 6),
            "longitude": round(float(lon), 6),
            "altitude_m": round(float(altitude_m), 1),
            "customs_status": customs_status,
            "flight_status": flight_status,
            "delay_hours": round(float(delay_hours), 2),
            "phase": phase,
            "weather_severity": round(severity, 3),
            # Route metadata — picked up by cascade_orchestrator._node_assess_risk()
            # so the reroute engine knows origin/destination for cold-storage lookup.
            "origin":      self.origin_name,
            "destination": self.destination_name,
            "carrier":     "pharma-air-sim",
            "product_id":  self.product_id,
        }


SIMS: Dict[str, Simulation] = {}

_approval_queue = ApprovalQueue()
_orchestrator   = CascadeOrchestrator(approval_queue=_approval_queue)
ORCHESTRATOR    = _orchestrator   # shared ref — overwritten by set_orchestrator() from start.py
_audit          = AuditLogger()

# Reroute results store — keyed by shipment_id, updated whenever a reroute plan executes
_REROUTE_STORE: Dict[str, dict] = {}


def set_queue(q: ApprovalQueue) -> None:
    """Wire in a shared ApprovalQueue (called from start.py)."""
    global _approval_queue
    _approval_queue = q


def set_orchestrator(orch: CascadeOrchestrator) -> None:
    """Wire in a shared CascadeOrchestrator (called from start.py)."""
    global _orchestrator, ORCHESTRATOR
    _orchestrator = orch
    ORCHESTRATOR  = orch
    _patch_reroute_handler(orch)


def _patch_reroute_handler(orch: CascadeOrchestrator) -> None:
    """Wrap the reroute action handler so results are stored in _REROUTE_STORE."""
    from agents.risk_agent import RecommendedAction as RA
    original = orch._handle_reroute_shipment

    def _patched(assessment):
        result = original(assessment)
        plan = (result or {}).get("reroute_plan") or {}
        if plan:
            _REROUTE_STORE[assessment.shipment_id] = {
                **plan,
                "shipment_id":   assessment.shipment_id,
                "assessed_at":   assessment.assessed_at.isoformat(),
                "risk_score":    round(assessment.risk_score, 4),
                "risk_level":    assessment.risk_level.value,
                "spoilage_prob": round(float(assessment.spoilage_prob or 0.0), 4),
                "product_id":    assessment.metadata.get("product_id", ""),
                "destination":   assessment.metadata.get("destination", ""),
                "battery_pct":   assessment.metadata.get("battery_pct"),
                "phase":         assessment.metadata.get("phase", ""),
                "delay_hours":   assessment.metadata.get("delay_hours", 0.0),
            }
        return result

    orch.act.register_handler(RA.REROUTE_SHIPMENT, lambda a: _patched(a))


# Patch the default orchestrator now that the function is defined
_patch_reroute_handler(_orchestrator)


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

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

    from data.product_catalogue import get_default_product_id
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
        product_id=req.product_id or get_default_product_id(),
        origin_weather_override=req.origin_weather,
        destination_weather_override=req.destination_weather,
        route_points=_route_points(origin, destination, segments=72),
    )
    SIMS[sim_id] = sim
    # Congestion: new sims join the runway queue for their origin.
    RUNWAY.enqueue(sim.origin_name, sim.sim_id)
    RUNWAY.reorder_by_priority(sim.origin_name, SIMS)

    def _telemetry_loop(s: Simulation) -> None:
        """Fast loop: update phase + telemetry every second so altitude changes smoothly."""
        while s.phase != "ARRIVED":
            time.sleep(1)
            s.update_phase(time.monotonic())
            tel = s.generate_telemetry()
            with s._pipeline_lock:
                s._last_telemetry = tel
        # One final write at ARRIVED so altitude shows 0.
        tel = s.generate_telemetry()
        with s._pipeline_lock:
            s._last_telemetry = tel

    def _orchestrator_loop(s: Simulation) -> None:
        """Slow loop: feed telemetry into the orchestrator every 4 s.
        ORCHESTRATOR.run() may block up to 30 s (HITL) but that never
        delays the telemetry loop above."""
        while True:
            time.sleep(4)
            with s._pipeline_lock:
                tel = s._last_telemetry
            if tel is None:
                continue
            state = ORCHESTRATOR.run(tel)
            with s._pipeline_lock:
                s._last_risk_state = state

            # If a REROUTE_SHIPMENT action was executed, apply it to the map
            if not s.rerouted and state.action_results:
                for r in state.action_results:
                    if (
                        r.success
                        and r.action.value == "REROUTE_SHIPMENT"
                        and isinstance(r.payload, dict)
                    ):
                        plan_dict = r.payload.get("reroute_plan") or r.payload
                        s.apply_reroute(plan_dict)
                        break

            if s.phase == "ARRIVED":
                return

    threading.Thread(target=_telemetry_loop,    args=(sim,), daemon=True).start()
    threading.Thread(target=_orchestrator_loop, args=(sim,), daemon=True).start()

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


class PriorityControlRequest(BaseModel):
    priority: int = Field(0, ge=0, le=100)
    reason: str = ""


@app.post("/api/sim/{sim_id}/priority")
def set_priority(sim_id: str, body: PriorityControlRequest) -> dict:
    """
    Set priority for a shipment waiting to take off.
    Higher priority jumps the runway queue for its origin.
    """
    sim = SIMS.get(sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation not found")
    sim.priority = int(body.priority)
    sim.priority_reason = (body.reason or "").strip()
    # Reorder runway queue for this origin to reflect new priority.
    RUNWAY.reorder_by_priority(sim.origin_name, SIMS)
    return {
        "sim_id": sim_id,
        "shipment_id": sim.shipment_id,
        "origin": sim.origin_name,
        "priority": sim.priority,
        "priority_reason": sim.priority_reason,
        "queue_position": RUNWAY.position(sim.origin_name, sim.sim_id),
        "congestion_level": RUNWAY.get_congestion_level(sim.origin_name),
    }


class CongestionControlRequest(BaseModel):
    origin: str
    congestion_level: int = Field(DEFAULT_CONGESTION_LEVEL, ge=0, le=5)


@app.post("/api/congestion")
def set_congestion(body: CongestionControlRequest) -> dict:
    """
    Control congestion level per origin.
    0 = none (fast departures) .. 5 = heavy (slow departures).
    """
    origin = (body.origin or "").strip()
    if not origin:
        raise HTTPException(status_code=422, detail="Missing origin")
    RUNWAY.set_congestion_level(origin, int(body.congestion_level))
    # Reorder queue to apply any current priorities.
    RUNWAY.reorder_by_priority(origin, SIMS)
    return {
        "origin": origin,
        "congestion_level": RUNWAY.get_congestion_level(origin),
        "service_interval_sec": RUNWAY.service_interval_sec(origin),
    }


@app.get("/api/congestion")
def list_congestion() -> List[dict]:
    """List congestion settings for origins seen so far."""
    out: List[dict] = []
    for origin in sorted({s.origin_name for s in SIMS.values()}):
        out.append(
            {
                "origin": origin,
                "congestion_level": RUNWAY.get_congestion_level(origin),
                "service_interval_sec": RUNWAY.service_interval_sec(origin),
            }
        )
    return out


@app.get("/api/sim/{sim_id}")
def get_sim(sim_id: str) -> dict:
    sim = SIMS.get(sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation not found")
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


@app.get("/api/reroute")
def get_reroute_results() -> List[dict]:
    """Return reroute plan results.  Both servers share one process so we can
    read directly from hitl.dashboard's in-memory store (written by the
    orchestrator's push_reroute_result call) without any cross-process IPC."""
    try:
        from hitl.dashboard import _reroute_results, _reroute_lock
        with _reroute_lock:
            plans = list(_reroute_results.values())
        return sorted(plans, key=lambda x: x.get("assessed_at", ""), reverse=True)
    except Exception:
        # Fallback to local store if dashboard module not available
        return sorted(_REROUTE_STORE.values(),
                      key=lambda x: x.get("assessed_at", ""), reverse=True)


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

    # Approve (or re-approve if already resolved by shared queue)
    updated = _approval_queue.approve(request_id, body.operator, actions, body.notes)
    _audit.log_hitl_decision(updated)

    sim = next((s for s in SIMS.values() if s.shipment_id == updated.shipment_id), None)

    # Use approved_actions from the updated request (handles both fresh and forwarded approvals)
    actions_to_execute = updated.approved_actions
    assessment = sim._last_assessment if sim else None

    if assessment is not None and actions_to_execute:
        results = _orchestrator.act.execute(assessment, actions_to_execute)
        if sim:
            sim._last_actions = [r.to_dict() for r in results]
        for r in results:
            _audit.log_action_result(r)
            # If the action is REROUTE_SHIPMENT and succeeded, redirect the flight on the map.
            if (
                sim is not None
                and r.success
                and r.action.value == "REROUTE_SHIPMENT"
                and isinstance(r.payload, dict)
            ):
                plan_dict = r.payload.get("reroute_plan") or r.payload
                sim.apply_reroute(plan_dict)
    elif sim is not None and not assessment:
        # No assessment cached — try to apply reroute from the reroute store
        import logging as _rl
        _rl.getLogger(__name__).warning(
            "[%s] No cached assessment for reroute — check _REROUTE_STORE",
            updated.shipment_id,
        )
        stored = _REROUTE_STORE.get(updated.shipment_id)
        if stored and not sim.rerouted:
            sim.apply_reroute(stored)

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
