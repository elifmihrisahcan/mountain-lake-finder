"""Small geometry helpers. Everything works on (lat, lon) tuples in degrees."""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

Point = Tuple[float, float]
EARTH_RADIUS_M = 6_371_000.0


def haversine_m(a: Point, b: Point) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def centroid(points: Sequence[Point]) -> Point:
    if not points:
        raise ValueError("centroid of empty geometry")
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


def bbox(points: Iterable[Point]) -> Tuple[float, float, float, float]:
    lats, lons = zip(*points)
    return min(lats), min(lons), max(lats), max(lons)


def bbox_distance_m(point: Point, box: Tuple[float, float, float, float]) -> float:
    """Lower bound on the distance from a point to anything inside a bbox."""
    s, w, n, e = box
    lat = min(max(point[0], s), n)
    lon = min(max(point[1], w), e)
    return haversine_m(point, (lat, lon))


def boxes_within(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float], pad_m: float) -> bool:
    """Cheap prefilter: could anything in box a be within pad_m of box b?"""
    pad_lat = math.degrees(pad_m / EARTH_RADIUS_M)
    mid_lat = math.radians((a[0] + a[2]) / 2)
    pad_lon = pad_lat / max(math.cos(mid_lat), 0.01)
    return not (a[2] + pad_lat < b[0] or b[2] + pad_lat < a[0] or a[3] + pad_lon < b[1] or b[3] + pad_lon < a[1])


def min_distance_m(a: Sequence[Point], b: Sequence[Point]) -> float:
    """Closest vertex-to-vertex distance. OSM shapes are densely noded, so this is
    a good-enough approximation of shape-to-shape distance for our purposes."""
    best = math.inf
    for p in a:
        for q in b:
            d = haversine_m(p, q)
            if d < best:
                best = d
    return best


def polygon_area_m2(ring: Sequence[Point]) -> float:
    """Approximate area of a closed ring via an equirectangular projection."""
    if len(ring) < 3:
        return 0.0
    lat0 = math.radians(centroid(ring)[0])
    xs = [math.radians(p[1]) * EARTH_RADIUS_M * math.cos(lat0) for p in ring]
    ys = [math.radians(p[0]) * EARTH_RADIUS_M for p in ring]
    s = 0.0
    for i in range(len(ring)):
        j = (i + 1) % len(ring)
        s += xs[i] * ys[j] - xs[j] * ys[i]
    return abs(s) / 2


def bbox_around(center: Point, radius_m: float) -> Tuple[float, float, float, float]:
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    dlon = math.degrees(radius_m / (EARTH_RADIUS_M * math.cos(math.radians(center[0]))))
    return center[0] - dlat, center[1] - dlon, center[0] + dlat, center[1] + dlon


def thin(points: List[Point], max_points: int = 200) -> List[Point]:
    """Downsample a long vertex list to keep the O(n*m) distance checks cheap."""
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    return [points[int(i * step)] for i in range(max_points)]


def point_in_ring(p: Point, ring: Sequence[Point]) -> bool:
    """Ray-casting point-in-polygon test (lat/lon treated as planar; fine at these scales)."""
    y, x = p
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


class PolygonIndex:
    """Grid index so 'which polygons contain this point?' doesn't scan every polygon."""

    def __init__(self, polygons: Sequence[Sequence[Sequence[Point]]], cell_deg: float = 0.05):
        self.cell = cell_deg
        self.polygons = polygons  # each polygon = list of rings
        self.grid: dict = {}
        for idx, rings in enumerate(polygons):
            pts = [q for r in rings for q in r]
            if not pts:
                continue
            s, w, n, e = bbox(pts)
            for i in range(int(math.floor(s / cell_deg)), int(math.floor(n / cell_deg)) + 1):
                for j in range(int(math.floor(w / cell_deg)), int(math.floor(e / cell_deg)) + 1):
                    self.grid.setdefault((i, j), []).append(idx)

    def contains(self, p: Point) -> bool:
        key = (int(math.floor(p[0] / self.cell)), int(math.floor(p[1] / self.cell)))
        return any(point_in_ring(p, r) for idx in self.grid.get(key, ()) for r in self.polygons[idx])


def sample_disc(center: Point, radius_m: float, n: int = 11) -> List[Point]:
    """Grid of roughly (pi/4)*n*n points evenly covering a disc."""
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    dlon = dlat / max(math.cos(math.radians(center[0])), 0.01)
    out = []
    for i in range(n):
        for j in range(n):
            u, v = (2 * i / (n - 1) - 1), (2 * j / (n - 1) - 1)
            if u * u + v * v <= 1:
                out.append((center[0] + u * dlat, center[1] + v * dlon))
    return out
