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
from typing import Dict, List, Optional, Tuple, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents.cascade_orchestrator import CascadeOrchestrator, PipelineState
from hitl.approval_queue import ApprovalQueue

# Shared HITL queue + orchestrator for the demo UI.
APPROVAL_QUEUE = ApprovalQueue(timeout_sec=30)
ORCHESTRATOR = CascadeOrchestrator(approval_queue=APPROVAL_QUEUE)


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
        "radius_km": float(rng.choice([40, 60, 80, 110])),
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
        "radius_km": float(rng.choice([40, 60, 80, 110])),
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
    # Simple linear interpolation is fine for demo distances.
    return (_lerp(a[0], b[0], t), _lerp(a[1], b[1], t))


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
    speed_multiplier: float = 60.0
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

    # runtime state
    phase: str = "WAIT_TAKEOFF"  # WAIT_TAKEOFF | ENROUTE | HOLDING | ARRIVED
    enroute_start_monotonic: Optional[float] = None
    holding_start_monotonic: Optional[float] = None
    arrived_at_monotonic: Optional[float] = None

    # congestion / priority (demo-only)
    # Higher priority moves a shipment ahead of others waiting to take off.
    # 0 = normal, 100 = emergency/critical.
    priority: int = 0
    priority_reason: str = ""

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
            if w_dest["severity"] < LANDING_BLOCK_SEVERITY and held_for >= MIN_HOLDING_SECONDS:
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
        # duration_seconds is the intended on-screen animation duration in real wall-clock
        # seconds.  speed_multiplier is NOT applied here — it only affects holding-circle
        # angular velocity.  Mixing it into the progress formula would make a 45 s flight
        # complete in 45/60 ≈ 0.75 s (invisible to the user).
        start = self.enroute_start_monotonic or now
        elapsed = now - start          # real wall-clock seconds since takeoff
        if self.duration_seconds <= 0:
            return 1.0
        linear = max(0.0, min(1.0, elapsed / self.duration_seconds))
        return _ease_in_out(linear)

    def current_position(self, now: float) -> Tuple[float, float]:
        self.update_phase(now)
        if self.phase == "WAIT_TAKEOFF":
            return self.origin
        if self.phase == "ENROUTE":
            return _interp_latlon(self.origin, self.destination, self.progress(now))
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

    def to_public(self) -> dict:
        now = time.monotonic()
        w_origin, w_dest = self.current_weather(now)
        lat, lon = self.current_position(now)
        dist_to_dest = _haversine_km((lat, lon), self.destination)

        # Journey timing:
        # - scheduled_start: sim creation (acts as scheduled departure in this demo)
        # - takeoff: when we enter ENROUTE
        # - arrival: when we enter ARRIVED
        takeoff_at = self.enroute_start_monotonic
        arrived_at = self.arrived_at_monotonic
        if arrived_at is not None:
            total_elapsed_sec = max(0.0, arrived_at - self.created_at_monotonic)
            airborne_sec = max(0.0, arrived_at - (takeoff_at or arrived_at))
            ground_delay_sec = max(0.0, (takeoff_at or arrived_at) - self.created_at_monotonic)
        else:
            total_elapsed_sec = max(0.0, now - self.created_at_monotonic)
            if takeoff_at is not None:
                airborne_sec = max(0.0, now - takeoff_at)
                ground_delay_sec = max(0.0, takeoff_at - self.created_at_monotonic)
            else:
                airborne_sec = 0.0
                ground_delay_sec = total_elapsed_sec

        with self._pipeline_lock:
            last_telemetry = self._last_telemetry
            last_risk_state = self._last_risk_state

        congestion_level = RUNWAY.get_congestion_level(self.origin_name)
        queue_position = RUNWAY.position(self.origin_name, self.sim_id) if self.phase == "WAIT_TAKEOFF" else None

        return {
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
            "journey_total_elapsed_sec": round(total_elapsed_sec, 1),   # scheduled -> arrival (or now)
            "journey_airborne_sec": round(airborne_sec, 1),             # takeoff -> arrival (or now)
            "journey_ground_delay_sec": round(ground_delay_sec, 1),     # scheduled -> takeoff (or until now)
            "telemetry": last_telemetry,
            "altitude_m": (last_telemetry or {}).get("altitude_m"),
            "last_risk_state": (
                last_risk_state.__dict__ if last_risk_state is not None else None
            ),
        }

    def generate_telemetry(self) -> dict:
        """
        Generate realistic cold-chain telemetry keyed to current phase and destination weather severity.
        Severity is expected in [0.0, 1.0].
        """
        now_mono = time.monotonic()
        # Telemetry should reflect the *current* phase; phase transitions are handled elsewhere.
        phase = self.phase

        w_origin, w_dest = self.current_weather(now_mono)
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

        # Map severity -> temperature excursion bands (conceptual mapping per spec).
        if severity < 0.5:
            temp_target = rng.uniform(4.0, 7.5)  # safe 2–8°C range (centered near 5°C)
        elif severity < 0.7:
            # Mild excursion 9–12°C
            t = (severity - 0.5) / 0.2
            temp_target = 9.0 + t * 3.0 + rng.uniform(-0.4, 0.4)
        else:
            # Serious excursion 13–18°C
            t = (severity - 0.7) / 0.3
            temp_target = 13.0 + t * 5.0 + rng.uniform(-0.6, 0.6)
        temp_target = max(2.0, min(18.5, temp_target))

        delay_hours = 0.0
        flight_status = "ON_TIME"
        customs_status = "CLEARED"

        if phase == "WAIT_TAKEOFF":
            # On ground, refrigeration stable, no delay reported yet.
            temperature_c = rng.uniform(4.0, 6.0)
            humidity_pct = rng.uniform(40.0, 55.0)
            shock_g = rng.uniform(0.02, 0.08)
            altitude_m = rng.uniform(0.0, 30.0)
            delay_hours = 0.0
            flight_status = "ON_TIME"
            customs_status = "CLEARED"
        elif phase == "ENROUTE":
            # In flight; if weather is severe, refrigeration is under stress and delays accumulate.
            temperature_c = temp_target
            humidity_pct = rng.uniform(18.0, 35.0)
            shock_g = rng.uniform(0.01, 0.06)
            # Smooth takeoff/climb and descent/approach profile based on eased progress.
            p = max(0.0, min(1.0, float(self.progress(now_mono))))
            ramp = 0.12  # ~12% of route for climb and descent
            climb = 1.0 if p >= ramp else (p / ramp)
            descend = 1.0 if p <= (1.0 - ramp) else ((1.0 - p) / ramp)
            alt_factor = max(0.0, min(1.0, min(climb, descend)))
            cruise_alt_m = 10600.0
            altitude_m = max(0.0, cruise_alt_m * alt_factor + rng.uniform(-120.0, 120.0))
            enroute_elapsed_h = max(
                0.0, (now_mono - (self.enroute_start_monotonic or now_mono)) / 3600.0
            )
            if severity > 0.6:
                delay_hours = enroute_elapsed_h * (severity - 0.6) * 6.0
            flight_status = "DELAYED" if delay_hours >= 0.5 else "ON_TIME"
            customs_status = "CLEARED"
        elif phase == "HOLDING":
            # Circling / diversion / customs hold: explicitly delayed and holding.
            customs_status = "HOLD"
            flight_status = "DELAYED"
            holding_elapsed_h = max(
                0.0, (now_mono - (self.holding_start_monotonic or now_mono)) / 3600.0
            )
            delay_hours = max(1.0, holding_elapsed_h + severity * 3.0)
            # Temperature keeps rising while time passes (cap at 18°C-ish).
            temperature_c = min(18.0, temp_target + holding_elapsed_h * 4.0 + rng.uniform(-0.3, 0.3))
            humidity_pct = rng.uniform(30.0, 50.0)
            shock_g = rng.uniform(0.02, 0.09)
            # Keep a realistic mid-altitude while circling; scale oscillation with speed_multiplier.
            osc = math.sin((now_mono - self.created_at_monotonic) * 0.35 * max(1.0, float(self.speed_multiplier)))
            altitude_m = max(0.0, 6100.0 + osc * 450.0 + rng.uniform(-80.0, 80.0))
        else:  # ARRIVED
            temperature_c = rng.uniform(4.0, 6.0)
            humidity_pct = rng.uniform(40.0, 60.0)
            shock_g = rng.uniform(0.01, 0.05)
            altitude_m = 0.0
            delay_hours = 0.0
            flight_status = "ON_TIME"
            customs_status = "CLEARED"

        return {
            "shipment_id": self.shipment_id,
            "container_id": f"CNT-{self.sim_id[:8]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "temperature_c": round(float(temperature_c), 2),
            "humidity_pct": round(float(humidity_pct), 1),
            "shock_g": round(float(shock_g), 3),
            "latitude": round(float(lat), 6),
            "longitude": round(float(lon), 6),
            "altitude_m": round(float(altitude_m), 1),
            "customs_status": customs_status,
            "flight_status": flight_status,
            "delay_hours": round(float(delay_hours), 2),
            "phase": phase,
            "weather_severity": round(severity, 3),
        }


SIMS: Dict[str, Simulation] = {}


app = FastAPI(title="Map Simulation (Isolated)", version="0.1.0")

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (static_dir / "index.html").read_text(encoding="utf-8")


@app.get("/api/options", response_model=RouteOptions)
def options() -> RouteOptions:
    # IMPORTANT: Dropdowns are intentionally driven by us-airports.csv `name` only.
    origins = AIRPORT_NAMES
    destinations = AIRPORT_NAMES
    source = str(PHARMA_ROUTES_CSV if PHARMA_ROUTES_CSV.exists() else SHIPMENT_CSV)
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
    return [s.to_public() for s in SIMS.values()]


# ---------------------------------------------------------------------------
# HITL endpoints — expose the shared ApprovalQueue over REST so the map UI
# (or any external client) can list, approve, and reject pending decisions
# without going through the separate HITL dashboard on port 8080.
# ---------------------------------------------------------------------------

class HITLApproveRequest(BaseModel):
    operator:         str  = "operator"
    approved_actions: Optional[List[str]] = None   # None = approve all
    notes:            str  = ""


class HITLRejectRequest(BaseModel):
    operator: str = "operator"
    notes:    str = ""


@app.get("/api/hitl/pending", tags=["HITL"])
def pending_hitl() -> List[dict]:
    """Return all approval requests currently waiting for a human decision."""
    return [r.to_dict() for r in APPROVAL_QUEUE.pending()]


@app.get("/api/hitl/all", tags=["HITL"])
def all_hitl() -> List[dict]:
    """Return every HITL request regardless of status (pending / approved / rejected / timeout)."""
    return [r.to_dict() for r in APPROVAL_QUEUE.all_requests()]


@app.post("/api/hitl/{request_id}/approve", tags=["HITL"])
def approve_hitl(request_id: str, body: HITLApproveRequest) -> dict:
    """
    Approve a pending HITL request.
    Pass approved_actions as a list of action strings to do a partial approval,
    or omit it (null) to approve all proposed actions.
    """
    from agents.risk_agent import RecommendedAction

    req = APPROVAL_QUEUE.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"Request '{request_id}' not found")
    if req.status != "PENDING":
        raise HTTPException(status_code=409, detail=f"Request is already {req.status}")

    actions = None
    if body.approved_actions is not None:
        try:
            actions = [RecommendedAction(a) for a in body.approved_actions]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    updated = APPROVAL_QUEUE.approve(request_id, body.operator, actions, body.notes)
    return updated.to_dict()


@app.post("/api/hitl/{request_id}/reject", tags=["HITL"])
def reject_hitl(request_id: str, body: HITLRejectRequest) -> dict:
    """Reject a pending HITL request."""
    req = APPROVAL_QUEUE.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"Request '{request_id}' not found")
    if req.status != "PENDING":
        raise HTTPException(status_code=409, detail=f"Request is already {req.status}")

    updated = APPROVAL_QUEUE.reject(request_id, body.operator, body.notes)
    return updated.to_dict()

