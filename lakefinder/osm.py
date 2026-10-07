"""Fetch features from OpenStreetMap (Overpass API) and Nominatim, and turn the
raw JSON into simple Feature objects."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import geo

USER_AGENT = "home-finder-lakefinder/0.1 (glacier lake scoring)"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"

ROCKY_NATURAL = {"scree", "bare_rock", "arete", "cliff", "ridge", "valley", "moraine", "rock", "stone"}


@dataclass
class Feature:
    osm_type: str
    osm_id: int
    kind: str  # place | peak | lake | glacier | rocky | stream
    tags: Dict[str, str]
    points: List[geo.Point]
    node_ids: List[int] = field(default_factory=list)
    rings: List[List[geo.Point]] = field(default_factory=list)  # closed outlines, for areas
    ele_estimate: Optional[float] = None  # from a DEM when OSM has no ele tag
    center: geo.Point = field(init=False)
    box: Tuple[float, float, float, float] = field(init=False)

    def __post_init__(self) -> None:
        self.center = geo.centroid(self.points)
        self.box = geo.bbox(self.points)

    @property
    def name(self) -> str:
        return self.tags.get("name:en") or self.tags.get("name") or ""

    @property
    def ele(self) -> Optional[float]:
        raw = self.tags.get("ele", "").replace(",", ".").split(" ")[0].rstrip("m")
        try:
            return float(raw)
        except ValueError:
            return self.ele_estimate

    @property
    def area_m2(self) -> float:
        return sum(geo.polygon_area_m2(r) for r in self.rings)

    @property
    def url(self) -> str:
        return f"https://www.openstreetmap.org/{self.osm_type}/{self.osm_id}"


def build_query(box: Tuple[float, float, float, float], timeout: int = 180) -> str:
    s, w, n, e = box
    b = f"({s},{w},{n},{e})"
    return f"""
[out:json][timeout:{timeout}];
(
  node["place"~"^(city|town|village|hamlet)$"]{b};
  node["natural"~"^(peak|volcano)$"]{b};
  way["natural"="water"]["water"~"^(lake|reservoir|lagoon)$"]{b};
  relation["natural"="water"]["water"~"^(lake|reservoir|lagoon)$"]{b};
  way["natural"="water"][!"water"]["name"]{b};
  way["natural"="glacier"]{b};
  relation["natural"="glacier"]{b};
  node["natural"="glacier"]{b};
  way["natural"~"^(scree|bare_rock|arete|cliff|ridge|valley|moraine)$"]{b};
  node["natural"~"^(arete|valley|moraine|rock)$"]{b};
  way["geological"="moraine"]{b};
  way["waterway"~"^(stream|river)$"]{b};
);
out geom;
"""


def _http(url: str, data: Optional[bytes] = None, timeout: int = 240) -> dict:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def overpass(query: str, retries: int = 2) -> dict:
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        for url in OVERPASS_URLS:
            try:
                return _http(url, urllib.parse.urlencode({"data": query}).encode())
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
                last_err = err
        time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"Overpass request failed: {last_err}")


def geocode(place: str) -> geo.Point:
    params = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
    results = _http(f"{NOMINATIM_URL}?{params}")
    if not results:
        raise ValueError(f"Could not geocode {place!r}")
    return float(results[0]["lat"]), float(results[0]["lon"])


def fill_elevations(features: List[Feature]) -> int:
    """Look up terrain elevation (Copernicus DEM via Open-Meteo) for features
    without an ele tag. Returns how many were filled; failures are non-fatal."""
    missing = [f for f in features if f.ele is None]
    filled = 0
    for i in range(0, len(missing), 100):
        batch = missing[i:i + 100]
        params = urllib.parse.urlencode({
            "latitude": ",".join(f"{f.center[0]:.5f}" for f in batch),
            "longitude": ",".join(f"{f.center[1]:.5f}" for f in batch),
        })
        try:
            values = _http(f"{ELEVATION_URL}?{params}", timeout=60).get("elevation", [])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            break
        for f, v in zip(batch, values):
            if v is not None:
                f.ele_estimate = float(v)
                filled += 1
    return filled


def _stitch(segments: List[List[geo.Point]]) -> List[List[geo.Point]]:
    """Join a multipolygon's member ways (which arrive in any order and
    direction) end-to-end into rings."""
    segs = [list(s) for s in segments if len(s) >= 2]
    rings: List[List[geo.Point]] = []
    while segs:
        ring = segs.pop(0)
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for i, seg in enumerate(segs):
                if seg[0] == ring[-1]:
                    ring += seg[1:]
                elif seg[-1] == ring[-1]:
                    ring += seg[-2::-1]
                elif seg[-1] == ring[0]:
                    ring = seg[:-1] + ring
                elif seg[0] == ring[0]:
                    ring = seg[:0:-1] + ring
                else:
                    continue
                segs.pop(i)
                changed = True
                break
        rings.append(ring)
    return rings


def _geometry(el: dict) -> Tuple[List[geo.Point], List[List[geo.Point]]]:
    """Return (all vertices, closed rings) for an Overpass element."""
    if el["type"] == "node":
        return [(el["lat"], el["lon"])], []
    if el["type"] == "way":
        pts = [(p["lat"], p["lon"]) for p in el.get("geometry", []) if p]
        return pts, ([pts] if len(pts) > 3 and pts[0] == pts[-1] else [])
    outer = [[(p["lat"], p["lon"]) for p in m["geometry"] if p]
             for m in el.get("members", [])
             if m.get("role", "outer") in ("outer", "") and m.get("geometry")]
    rings = _stitch(outer)
    return [p for r in rings for p in r], [r for r in rings if len(r) > 3 and r[0] == r[-1]]


def _classify(tags: Dict[str, str]) -> Optional[str]:
    natural = tags.get("natural")
    if "place" in tags:
        return "place"
    if natural in ("peak", "volcano"):
        return "peak"
    if natural == "glacier":
        return "glacier"
    if natural == "water":
        return "lake"
    if natural in ROCKY_NATURAL or tags.get("geological") == "moraine":
        return "rocky"
    if tags.get("waterway") in ("stream", "river"):
        return "stream"
    return None


def parse(data: dict) -> Dict[str, List[Feature]]:
    out: Dict[str, List[Feature]] = {k: [] for k in ("place", "peak", "lake", "glacier", "rocky", "stream")}
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        kind = _classify(tags)
        pts, rings = _geometry(el)
        if kind and pts:
            out[kind].append(Feature(el["type"], el["id"], kind, tags, pts, el.get("nodes", []), rings))
    return out
