"""
sea_routes.py
-------------
Maritime route generation using the `searoute` library, which computes
actual shipping-lane paths through real-world maritime chokepoints
(Suez, Malacca, Gibraltar, Panama, Cape of Good Hope, etc.).

No API key needed — searoute uses an embedded graph of global shipping lanes.

For air routes, great-circle interpolation is used.
"""

from __future__ import annotations

import logging
import math
from typing import List, Tuple

logger = logging.getLogger(__name__)

try:
    import searoute as sr
    _HAS_SEAROUTE = True
except ImportError:
    _HAS_SEAROUTE = False
    logger.warning("searoute not installed — sea routes will use straight lines. "
                   "Install with: pip install searoute")


def build_sea_route(
    origin: Tuple[float, float],
    destination: Tuple[float, float],
    points_per_segment: int = 15,
) -> List[Tuple[float, float]]:
    """
    Build a realistic sea route from origin to destination.

    Uses the searoute library for actual maritime lane routing.
    Falls back to straight-line interpolation if searoute is unavailable.

    Args:
        origin: (lat, lon)
        destination: (lat, lon)
        points_per_segment: ignored when using searoute (it returns its own density)

    Returns:
        List of (lat, lon) waypoints.
    """
    if not _HAS_SEAROUTE:
        return _straight_line(origin, destination, points_per_segment * 3)

    try:
        # searoute uses GeoJSON order: [lon, lat]
        o = [origin[1], origin[0]]
        d = [destination[1], destination[0]]

        route = sr.searoute(o, d)
        coords = route["geometry"]["coordinates"]

        # Convert from [lon, lat] back to (lat, lon)
        # Keep longitude continuous (don't wrap to [-180,180]) so Leaflet
        # draws Pacific-crossing routes correctly without jumping.
        path = [(c[1], c[0]) for c in coords]

        if len(path) < 2:
            return _straight_line(origin, destination, points_per_segment * 3)

        logger.debug(
            "Sea route %s → %s: %d points, %.0f km",
            origin, destination, len(path),
            route.get("properties", {}).get("length", 0),
        )
        return path

    except Exception as exc:
        logger.warning("searoute failed (%s) — falling back to straight line", exc)
        return _straight_line(origin, destination, points_per_segment * 3)


def build_air_route(
    origin: Tuple[float, float],
    destination: Tuple[float, float],
    n_points: int = 60,
) -> List[Tuple[float, float]]:
    """Great-circle interpolation for air routes."""
    lat1_r, lon1_r = math.radians(origin[0]), math.radians(origin[1])
    lat2_r, lon2_r = math.radians(destination[0]), math.radians(destination[1])

    d = math.acos(
        min(1.0, max(-1.0,
            math.sin(lat1_r) * math.sin(lat2_r) +
            math.cos(lat1_r) * math.cos(lat2_r) * math.cos(lon2_r - lon1_r)
        ))
    )
    if d < 1e-10:
        return [origin] * n_points

    points = []
    for i in range(n_points):
        f = i / max(n_points - 1, 1)
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(lat1_r) * math.cos(lon1_r) + b * math.cos(lat2_r) * math.cos(lon2_r)
        y = a * math.cos(lat1_r) * math.sin(lon1_r) + b * math.cos(lat2_r) * math.sin(lon2_r)
        z = a * math.sin(lat1_r) + b * math.sin(lat2_r)
        lat = math.degrees(math.atan2(z, math.sqrt(x * x + y * y)))
        lon = math.degrees(math.atan2(y, x))
        points.append((round(lat, 5), round(lon, 5)))
    return points


def _straight_line(
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    n: int,
) -> List[Tuple[float, float]]:
    """Simple linear interpolation fallback."""
    points = []
    for i in range(n):
        t = i / max(n - 1, 1)
        lat = p1[0] + t * (p2[0] - p1[0])
        lon = p1[1] + t * (p2[1] - p1[1])
        points.append((round(lat, 5), round(lon, 5)))
    return points
