"""Scan a whole country by splitting it into tiles.

1. Look up the country's OSM boundary (Nominatim) and fetch every peak in it.
2. Keep only tiles with mountains in them (most of a country is flat).
3. For each tile, run a lean Overpass query: lakes and places inside the country,
   plus only the peaks, glaciers, rocky terrain and streams that can affect them.
   Each tile is padded so lakes near a tile edge still see their mountains,
   and every tile response is cached so an interrupted scan resumes.
4. Score each tile and merge, keeping a lake/place only in the tile its centre falls in.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.parse
from collections import Counter
from typing import Dict, List, Optional, Tuple

from . import geo, osm
from .scoring import (LakeAssessment, LakeScore, LandCover, PlaceScore, Settings, assess_lakes, score_lakes,
                      score_places)

Box = Tuple[float, float, float, float]


def country_area_id(name: str) -> Tuple[int, str]:
    """Overpass area id and display name for a country name or ISO code."""
    params = urllib.parse.urlencode({"q": name, "format": "json", "limit": 5, "featureType": "country"})
    for r in osm._http(f"{osm.NOMINATIM_URL}?{params}"):
        if r.get("osm_type") == "relation":
            return 3_600_000_000 + int(r["osm_id"]), r.get("display_name", name)
    raise ValueError(f"Could not find a country boundary for {name!r}")


def plan_tiles(area_id: int, tile_deg: float, min_peaks: int) -> List[Box]:
    data = osm.overpass(f'[out:json][timeout:300];area({area_id})->.c;'
                        f'node["natural"~"^(peak|volcano)$"](area.c);out skel qt;')
    counts = Counter((math.floor(e["lat"] / tile_deg), math.floor(e["lon"] / tile_deg))
                     for e in data["elements"])
    tiles = [(i * tile_deg, j * tile_deg, (i + 1) * tile_deg, (j + 1) * tile_deg)
             for (i, j), n in counts.items() if n >= min_peaks]
    return sorted(tiles, key=lambda b: (-b[2], b[1]))  # north to south, west to east


def tile_query(area_id: int, core: Box, s: Settings) -> str:
    """Lakes/places from a padded box inside the country; supporting features
    only where they can matter (around those lakes)."""
    pad_lat = s.mountain_km / 111
    pad_lon = pad_lat / max(math.cos(math.radians((core[0] + core[2]) / 2)), 0.2)
    p = (core[0] - pad_lat, core[1] - pad_lon, core[2] + pad_lat, core[3] + pad_lon)
    b = f"({p[0]:.4f},{p[1]:.4f},{p[2]:.4f},{p[3]:.4f})"
    gk, rk, sk = int(s.glacier_km * 1000), int(s.rocky_km * 1000), int((s.glacier_km + 3) * 1000)
    lk = int(s.green_km * 1000)
    return f"""
[out:json][timeout:180][maxsize:268435456];
area({area_id})->.c;
(
  way["natural"="water"]["water"~"^(lake|reservoir|lagoon)$"]{b}(area.c);
  relation["natural"="water"]["water"~"^(lake|reservoir|lagoon)$"]{b}(area.c);
  way["natural"="water"][!"water"]["name"]{b}(area.c);
)->.lakes;
node["place"~"^(city|town|village|hamlet)$"]{b}(area.c)->.places;
node["natural"~"^(peak|volcano)$"]{b}->.peaks;
(
  way["natural"="glacier"](around.lakes:{gk});
  relation["natural"="glacier"](around.lakes:{gk});
)->.glaciers;
(
  way["natural"~"^(scree|bare_rock|arete|cliff|ridge|valley|moraine)$"](around.lakes:{rk});
  node["natural"~"^(arete|valley|moraine|rock)$"](around.lakes:{rk});
  way["geological"="moraine"](around.lakes:{rk});
)->.rocky;
way["waterway"~"^(stream|river)$"](around.glaciers:{sk})->.streams;
(
{osm.landcover(f"(around.lakes:{lk})")}
{osm.landcover(f"(around.places:{lk})")}
)->.cover;
(.lakes; .places; .peaks; .glaciers; .rocky; .streams; .cover;);
out geom;
"""


def _inside(p: geo.Point, core: Box) -> bool:
    return core[0] <= p[0] < core[2] and core[1] <= p[1] < core[3]


def scan(
    country: str,
    s: Settings,
    cache_dir: str,
    tile_deg: float = 1.0,
    min_peaks: int = 3,
    max_tiles: Optional[int] = None,
    elevation: bool = True,
    require: bool = True,
) -> Tuple[List[PlaceScore], List[LakeAssessment], List[LakeScore]]:
    os.makedirs(cache_dir, exist_ok=True)
    plan_path = os.path.join(cache_dir, "plan.json")
    if os.path.exists(plan_path):
        with open(plan_path) as f:
            plan = json.load(f)
    else:
        area_id, label = country_area_id(country)
        print(f"Country: {label} (area {area_id}). Finding mountain tiles...", file=sys.stderr)
        plan = {"area_id": area_id, "label": label, "tile_deg": tile_deg,
                "tiles": plan_tiles(area_id, tile_deg, min_peaks)}
        with open(plan_path, "w") as f:
            json.dump(plan, f)
    tiles = [tuple(t) for t in plan["tiles"]][:max_tiles]
    print(f"{plan['label']}: {len(tiles)} mountain tiles of {plan['tile_deg']}°", file=sys.stderr)

    places: Dict[int, PlaceScore] = {}
    lakes: Dict[int, LakeAssessment] = {}
    boards: Dict[int, LakeScore] = {}
    failed: List[Box] = []
    queue = [(n, core, False) for n, core in enumerate(tiles, 1)]
    while queue:
        n, core, retry = queue.pop(0)
        path = os.path.join(cache_dir, f"tile_{core[0]:+.2f}_{core[1]:+.2f}.json")
        t0 = time.time()
        if os.path.exists(path):
            with open(path) as f:
                raw = json.load(f)
            source = "cached"
        else:
            try:
                raw = osm.overpass(tile_query(plan["area_id"], core, s))
            except RuntimeError as err:
                if retry:
                    failed.append(core)
                    print(f"  [{n}/{len(tiles)}] {core}: failed again ({err}); rerun later to retry",
                          file=sys.stderr)
                else:
                    queue.append((n, core, True))  # second pass at the end, when the server may be calmer
                    print(f"  [{n}/{len(tiles)}] {core}: server busy, will retry at the end", file=sys.stderr)
                    time.sleep(30)
                continue
            with open(path, "w") as f:
                json.dump(raw, f)
            source = "fetched"
            time.sleep(2)  # be gentle with the public Overpass servers

        features = osm.parse(raw)
        if elevation:
            osm.fill_elevations(features["lake"] + features["glacier"])
        assessed = assess_lakes(features, s)
        cover = LandCover(features, s)
        for r in score_lakes(features, assessed, s, require_mountains=require, cover=cover):
            if _inside(r.lake.lake.center, core):
                boards.setdefault(r.lake.lake.osm_id, r)
        for la in assessed:
            if _inside(la.lake.center, core):
                lakes.setdefault(la.lake.osm_id, la)
        results, _ = score_places(features, s, require_both=require, assessed=assessed, cover=cover)
        for r in results:
            if _inside(r.place.center, core):
                places.setdefault(r.place.osm_id, r)
        blue = sum(la.is_blue for la in assessed if _inside(la.lake.center, core))
        print(f"  [{n}/{len(tiles)}] {core[0]:.0f},{core[1]:.0f} {source} {time.time() - t0:.0f}s: "
              f"{len(features['lake'])} lakes, {blue} blue, {len(features['peak'])} peaks", file=sys.stderr)

    if failed:
        print(f"{len(failed)} tiles could not be downloaded; run the same command again to fill them in.",
              file=sys.stderr)
    return (sorted(places.values(), key=lambda r: r.score, reverse=True),
            list(lakes.values()),
            sorted(boards.values(), key=lambda r: r.score, reverse=True))
