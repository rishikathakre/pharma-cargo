from __future__ import annotations

import csv
import json
import math
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sys

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]

# Make project root importable so we can reach hitl.sea_routes
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hitl.sea_routes import build_sea_route
DATA_RAW = ROOT / "data" / "raw"

# Prefer pharma routes if present; otherwise fall back to shipment.csv.
PHARMA_ROUTES_CSV = DATA_RAW / "pharma_routes.csv"
SHIPMENT_CSV = DATA_RAW / "shipment.csv"

# Ports dataset (JSON).
# Expected schema: list of objects with keys:
#   CITY, STATE, COUNTRY, LATITUDE, LONGITUDE
PORTS_JSON = DATA_RAW / "ports.json"

# Small, editable coordinate lookup for demos.
# TODO: Replace/extend with an airport/port coordinates dataset or a geocoder.
COORDS: Dict[str, Tuple[float, float]] = {
    # Airports / cities (demo-friendly)
    "JFK Airport": (40.6413, -73.7781),
    "Heathrow Airport": (51.4700, -0.4543),
    "Frankfurt Airport": (50.0379, 8.5622),
    "Mumbai Airport": (19.0896, 72.8656),
    "O'Hare Airport": (41.9742, -87.9073),
    "Pudong Airport": (31.1443, 121.8083),
    "Singapore Changi Airport": (1.3644, 103.9915),
    # Ports (demo-friendly)
    "Port of Rotterdam": (51.95, 4.14),
    "Port of Singapore": (1.2644, 103.8222),
    "Port of Los Angeles": (33.7366, -118.2626),
    "Port of Long Beach": (33.7542, -118.2165),
}

def _load_ports(
    path: Path,
    *,
    non_usa_count: int = 20,
    usa_count: int = 6,
    seed: int = 42,
) -> tuple[Dict[str, Tuple[float, float]], List[str]]:
    """
    Load port name -> (lat, lon) from ports.json and return a curated demo subset
    of major coastal ports that produce good sea routes.
    """
    # Curated list of major ports that work well with searoute
    curated: Dict[str, Tuple[float, float]] = {
        "Rotterdam (Netherlands)":       (51.95, 4.14),
        "Singapore (Singapore)":         (1.26, 103.82),
        "Shanghai (China)":              (31.36, 121.59),
        "Yokohama (Japan)":              (35.44, 139.64),
        "Busan (South Korea)":           (35.08, 129.08),
        "Los Angeles (U.S.A.)":          (33.74, -118.26),
        "New York (U.S.A.)":             (40.67, -74.04),
        "Houston (U.S.A.)":              (29.76, -95.09),
        "Santos (Brazil)":               (-23.96, -46.33),
        "Dubai / Jebel Ali (U.A.E.)":    (25.02, 55.06),
        "Mumbai (India)":                (18.93, 72.83),
        "Algeciras (Spain)":             (36.13, -5.44),
        "Hamburg (Germany)":             (53.53, 9.97),
        "Antwerp (Belgium)":             (51.30, 4.36),
        "Felixstowe (U.K.)":            (51.96, 1.30),
        "Durban (South Africa)":         (-29.86, 31.02),
        "Melbourne (Australia)":         (-37.82, 144.91),
        "Colombo (Sri Lanka)":           (6.95, 79.84),
        "Piraeus (Greece)":              (37.94, 23.64),
        "Cartagena (Colombia)":          (10.39, -75.51),
        "Colon (Panama)":               (9.36, -79.90),
        "Kingston (Jamaica)":            (17.97, -76.79),
        "Dakar (Senegal)":              (14.69, -17.44),
        "Cape Town (South Africa)":      (-33.92, 18.42),
        "Ho Chi Minh City (Vietnam)":    (10.77, 106.70),
        "Laem Chabang (Thailand)":       (13.08, 100.88),
    }

    names = sorted(curated.keys())
    return curated, names


PORT_COORDS, PORT_NAMES = _load_ports(PORTS_JSON)


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
    ports_indexed: int = 0
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
    route_path: List[Tuple[float, float]]  # full waypoint path

    def progress(self) -> float:
        elapsed = (time.monotonic() - self.created_at_monotonic) * self.speed_multiplier
        if self.duration_seconds <= 0:
            return 1.0
        return max(0.0, min(1.0, elapsed / self.duration_seconds))

    def current_position(self) -> Tuple[float, float]:
        p = self.progress()
        path = self.route_path
        if not path or len(path) < 2:
            return _interp_latlon(self.origin, self.destination, p)
        # Interpolate along the multi-point path
        total = len(path) - 1
        exact = p * total
        idx = int(exact)
        frac = exact - idx
        if idx >= total:
            return path[-1]
        return _interp_latlon(path[idx], path[idx + 1], frac)

    def to_public(self) -> dict:
        lat, lon = self.current_position()
        return {
            "sim_id": self.sim_id,
            "shipment_id": self.shipment_id,
            "origin": self.origin_name,
            "destination": self.destination_name,
            "progress": round(self.progress(), 4),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "distance_km": round(_haversine_km(self.origin, self.destination), 1),
            "route_points": len(self.route_path),
        }


SIMS: Dict[str, Simulation] = {}


app = FastAPI(title="Map Simulation (Isolated)", version="0.1.0")

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (static_dir / "index_ship.html").read_text(encoding="utf-8")


@app.get("/api/options", response_model=RouteOptions)
def options() -> RouteOptions:
    # IMPORTANT: Dropdowns are intentionally driven by ports.json subset only.
    origins = PORT_NAMES
    destinations = PORT_NAMES
    source = str(PHARMA_ROUTES_CSV if PHARMA_ROUTES_CSV.exists() else SHIPMENT_CSV)
    return RouteOptions(
        origins=origins,
        destinations=destinations,
        coords_known=sorted(COORDS.keys()),
        ports_indexed=len(PORT_COORDS),
        routes_count=len(ROUTES_CACHE),
        source_file=source,
    )


@app.get("/api/lookup")
def lookup(name: str) -> dict:
    """
    Resolve a location name to coordinates.

    Sources (in order):
      1) Built-in COORDS (small curated demo set)
      2) PORT_COORDS (from data/raw/ports.json demo subset)
    """
    key = (name or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="Missing name")
    if key in COORDS:
        lat, lon = COORDS[key]
        return {"found": True, "name": key, "lat": lat, "lon": lon, "source": "builtin"}
    if key in PORT_COORDS:
        lat, lon = PORT_COORDS[key]
        return {"found": True, "name": key, "lat": lat, "lon": lon, "source": "ports_json"}
    return {
        "found": False,
        "name": key,
        "detail": (
            "Not found. Provide lat/lon manually, add to COORDS in map_sim/app.py, "
            "or update data/raw/ports.json."
        ),
    }


def _resolve_point(name: str, lat: Optional[float], lon: Optional[float]) -> Tuple[float, float]:
    if name in COORDS:
        return COORDS[name]
    if name in PORT_COORDS:
        return PORT_COORDS[name]
    if lat is not None and lon is not None:
        return (float(lat), float(lon))
    raise HTTPException(
        status_code=422,
        detail=(
            f"Unknown location '{name}'. Provide origin_lat/origin_lon or "
            "add it to COORDS in map_sim/app.py, or add it to data/raw/ports.json."
        ),
    )


@app.post("/api/sim", response_model=CreateSimResponse)
def create_sim(req: CreateSimRequest) -> CreateSimResponse:
    sim_id = str(uuid.uuid4())
    shipment_id = req.shipment_id or f"SHP-MAP-{sim_id[:8]}"

    origin = _resolve_point(req.origin, req.origin_lat, req.origin_lon)
    destination = _resolve_point(req.destination, req.destination_lat, req.destination_lon)

    # Build realistic sea route through shipping lanes
    route_path = build_sea_route(origin, destination, points_per_segment=15)

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
        route_path=route_path,
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
    )


@app.get("/api/sim/{sim_id}")
def get_sim(sim_id: str) -> dict:
    sim = SIMS.get(sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation not found")
    return sim.to_public()


@app.get("/api/sim/{sim_id}/route")
def get_sim_route(sim_id: str) -> dict:
    """Return the full sea-route path for drawing on the map."""
    sim = SIMS.get(sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation not found")
    return {
        "sim_id": sim_id,
        "origin": sim.origin_name,
        "destination": sim.destination_name,
        "points": len(sim.route_path),
        "path": [[lat, lon] for lat, lon in sim.route_path],
    }


@app.get("/api/sims")
def list_sims() -> List[dict]:
    return [s.to_public() for s in SIMS.values()]

