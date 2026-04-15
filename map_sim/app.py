from __future__ import annotations

import csv
import math
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


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

# How often dummy weather changes (wall-clock seconds).
WEATHER_REFRESH_SEC = 6.0

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

    # runtime state
    phase: str = "WAIT_TAKEOFF"  # WAIT_TAKEOFF | ENROUTE | HOLDING | ARRIVED
    enroute_start_monotonic: Optional[float] = None
    arrived_at_monotonic: Optional[float] = None

    def _bucket(self, now: float) -> int:
        return int(max(0.0, now - self.created_at_monotonic) / WEATHER_REFRESH_SEC)

    def current_weather(self, now: float) -> tuple[dict, dict]:
        b = self._bucket(now)
        return (
            _weather_for(self.weather_seed, b, "origin"),
            _weather_for(self.weather_seed, b, "destination"),
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
        return max(0.0, min(1.0, elapsed / self.duration_seconds))

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
            angle = (now - self.created_at_monotonic) * 1.4
            return (lat0 + math.sin(angle) * radius_deg, lon0 + math.cos(angle) * radius_deg)
        # ARRIVED
        return self.destination

    def to_public(self) -> dict:
        now = time.monotonic()
        w_origin, w_dest = self.current_weather(now)
        lat, lon = self.current_position(now)
        dist_to_dest = _haversine_km((lat, lon), self.destination)
        return {
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
    w_origin = _weather_for(seed, b, "origin")
    w_dest = _weather_for(seed, b, "destination")

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
    )


@app.get("/api/sim/{sim_id}")
def get_sim(sim_id: str) -> dict:
    sim = SIMS.get(sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation not found")
    return sim.to_public()


@app.get("/api/sims")
def list_sims() -> List[dict]:
    return [s.to_public() for s in SIMS.values()]

