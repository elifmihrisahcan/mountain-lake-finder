"""Glacial-lake detection and place scoring.

A lake looks "blue" (glacier-fed: rock flour from glaciers grinding through
rocky valleys upstream scatters light into turquoise/blue) when the map shows:

  * an active glacier nearby and upslope of the lake,
  * rocky glacial terrain around it (scree, bare rock, moraines, aretes, cliffs),
  * a stream that runs from the glacier down into the lake,
  * glacier / colour words in the labels ("Gletschersee", "Lago Azul", "Turquoise Lake"...),
  * high elevation.

Each signal adds to a 0..1 confidence score, and blue lakes earn bonus points
for the places near them.
"""
from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import geo
from .osm import Feature

GLACIER_WORDS = re.compile(
    r"glaci|gletscher|ghiacci|glaciar|glacj|lodowc|ledovec|jökul|jokul|isbre|\bbre\b|bræ|"
    r"jäkk|jiekk|buzul|ледник|льодов|冰川|氷河|빙하",
    re.IGNORECASE,
)
BLUE_WORDS = re.compile(
    r"blue|blau|bleu|blu\b|azul|azzurr|türkis|turquoise|turchese|turquesa|emerald|smaragd|"
    r"mavi|turkuaz|zümrüt|modr|niebiesk|голуб|сини|бирюз|kék|sininen|blå",
    re.IGNORECASE,
)
BLUE_TAG_VALUES = re.compile(r"blue|turquoise|cyan|teal|milky|azure|glacial|glacier", re.IGNORECASE)
COLOUR_TAGS = ("colour", "color", "water:colour", "water_colour", "lake:type", "glacial", "origin", "water")


@dataclass
class Settings:
    glacier_km: float = 5.0          # how far a glacier may be from a lake to count
    rocky_km: float = 2.0            # radius to look for scree / moraine / bare rock
    stream_touch_m: float = 200.0    # stream counts as touching a lake within this
    glacier_source_m: float = 600.0  # ...or as starting at a glacier (they retreat faster than maps update)
    stream_gap_m: float = 40.0       # join streams whose ends are this close even without a shared node
    stream_max_hops: int = 10        # downstream way-to-way hops followed from a glacier
    high_lake_m: float = 1500.0      # lakes above this elevation get a small boost
    mountain_km: float = 10.0        # peaks counted around a place
    lake_km: float = 10.0            # lakes counted around a place
    min_lake_ha: float = 1.0         # ignore ponds smaller than this
    blue_threshold: float = 0.5      # confidence needed to call a lake "blue"
    likely_factor: float = 0.5       # bonus share for "likely blue" (glacier above, but no traced stream)
    # Points shrink linearly with distance: full value next door, ~0 at the radius edge.
    peak_points: float = 1.0         # per peak, +1 per 1000 m above 1000 m elevation
    lake_points: float = 4.0
    big_lake_points: float = 2.0     # extra for lakes > 50 ha
    blue_lake_points: float = 10.0   # the "+ point" for blue lakes (scaled by confidence)
    max_peak_points: float = 25.0
    max_lakes_counted: int = 5
    # Green surroundings: forest, meadow, grassland... are a plus; bare rock, scree, sand, ice a minus.
    green_km: float = 2.0            # look this far around a place / a lake's shore
    green_points: float = 5.0        # +5 when everything around is green
    barren_points: float = 5.0       # -5 when everything around is barren


@dataclass
class Cover:
    """Share of the land (water excluded) around a spot that is green / barren.
    The rest is land with no mapped cover, which counts as neutral."""
    green: float = 0.0
    barren: float = 0.0
    points: float = 0.0

    def describe(self) -> str:
        return f"{self.green:.0%} green, {self.barren:.0%} barren"


@dataclass
class LakeAssessment:
    lake: Feature
    area_ha: float
    confidence: float = 0.0
    is_blue: bool = False            # glacier-fed and confirmed (stream / label / tag)
    is_likely_blue: bool = False     # glacier upslope + rocky terrain, but feeding not traced
    evidence: List[str] = field(default_factory=list)
    nearest_glacier_km: Optional[float] = None


@dataclass
class PlaceScore:
    place: Feature
    score: float
    peak_points: float
    lake_points: float
    blue_points: float
    peaks: List[Feature]
    lakes: List[LakeAssessment]
    lake_distances_km: Dict[int, float]
    cover: Cover = field(default_factory=Cover)


# ---------------------------------------------------------------- streams ---

def glacier_fed_streams(streams: List[Feature], glaciers: List[Feature], s: Settings) -> Dict[int, Tuple[str, str]]:
    """Return {stream index: (glacier name, stream name)} for every stream
    reachable downstream from a stream that starts at a glacier. Unnamed
    stretches inherit the name of the nearest named stretch upstream.

    OSM waterways are drawn in flow direction, so a way whose last node is part
    of another way flows into it.
    """
    if not streams or not glaciers:
        return {}
    by_node: Dict[int, List[int]] = {}
    for i, st in enumerate(streams):
        for n in st.node_ids:
            by_node.setdefault(n, []).append(i)

    def downstream(i: int) -> List[int]:
        st = streams[i]
        if st.node_ids and by_node.get(st.node_ids[-1], [i]) != [i]:
            return [j for j in by_node[st.node_ids[-1]] if j != i]
        # Not joined in OSM: fall back to any stream passing right by our mouth.
        mouth = st.points[-1]
        return [j for j, other in enumerate(streams)
                if j != i and geo.bbox_distance_m(mouth, other.box) <= s.stream_gap_m
                and geo.min_distance_m([mouth], other.points) <= s.stream_gap_m]

    sources: Dict[int, str] = {}
    for i, st in enumerate(streams):
        for g in glaciers:
            if not geo.boxes_within(st.box, g.box, s.glacier_source_m):
                continue
            # The stream's upper end (first ~third) must start at the glacier.
            head = st.points[: max(2, len(st.points) // 3)]
            if geo.min_distance_m(geo.thin(head, 60), geo.thin(g.points, 150)) <= s.glacier_source_m:
                sources[i] = g.name or "unnamed glacier"
                break

    fed: Dict[int, Tuple[str, str]] = {}
    queue = deque((i, 0, name, "") for i, name in sources.items())
    while queue:
        i, hops, name, upstream_label = queue.popleft()
        label = streams[i].name or upstream_label
        if i in fed and (fed[i][1] or not label):
            continue  # already reached; only revisit to fill in a missing name
        fed[i] = (name, label)
        if hops >= s.stream_max_hops:
            continue
        for j in downstream(i):
            if j not in fed or (label and not fed[j][1]):
                queue.append((j, hops + 1, name, label))
    return fed


# ------------------------------------------------------------------ lakes ---

def _lake_area_ha(lake: Feature) -> float:
    return lake.area_m2 / 10_000


def assess_lake(
    lake: Feature,
    glaciers: List[Feature],
    rocky: List[Feature],
    streams: List[Feature],
    fed_streams: Dict[int, Tuple[str, str]],
    s: Settings,
) -> LakeAssessment:
    a = LakeAssessment(lake=lake, area_ha=_lake_area_ha(lake))
    shore = geo.thin(lake.points, 150)
    conf = 0.0
    glacier_signal = False
    rocky_signal = False
    confirmed = False  # evidence that the glacier really drains into this lake

    # 1. Labels: glacier words / blue colour words in the lake's names
    names = " ".join(v for k, v in lake.tags.items() if k.startswith("name") or k in ("alt_name", "official_name"))
    if GLACIER_WORDS.search(names):
        conf += 0.35
        glacier_signal = confirmed = True
        a.evidence.append("name mentions a glacier")
    if BLUE_WORDS.search(names):
        conf += 0.15
        a.evidence.append("name mentions blue/turquoise colour")

    # 2. Explicit colour / origin tags
    for key in COLOUR_TAGS:
        val = lake.tags.get(key, "")
        pattern = GLACIER_WORDS if key == "water" else BLUE_TAG_VALUES  # water=lake is not a colour
        if val and pattern.search(val):
            conf += 0.3
            if "glac" in val.lower():
                glacier_signal = confirmed = True
            a.evidence.append(f"tagged {key}={val}")
            break

    # 3. Active glacier nearby and upslope
    best = math.inf
    best_glacier: Optional[Feature] = None
    for g in glaciers:
        if not geo.boxes_within(lake.box, g.box, s.glacier_km * 1000):
            continue
        d = geo.min_distance_m(shore, geo.thin(g.points, 150))
        if d < best:
            best, best_glacier = d, g
    if best_glacier is not None and best <= s.glacier_km * 1000:
        a.nearest_glacier_km = round(best / 1000, 2)
        known = lake.ele is not None and best_glacier.ele is not None
        upslope = known and best_glacier.ele > lake.ele + 50
        if upslope or not known:
            conf += 0.3 * (1 - 0.5 * best / (s.glacier_km * 1000))
            glacier_signal = True
            label = best_glacier.name or "a glacier"
            a.evidence.append(f"{label} {a.nearest_glacier_km} km away")
        if upslope:
            conf += 0.1
            a.evidence.append(f"glacier sits {best_glacier.ele - lake.ele:.0f} m above the lake")

    # 4. A meltwater stream runs from a glacier into the lake
    for i, (glacier_name, stream_label) in fed_streams.items():
        st = streams[i]
        if not geo.boxes_within(lake.box, st.box, s.stream_touch_m):
            continue
        if geo.min_distance_m(geo.thin(st.points, 80), shore) <= s.stream_touch_m:
            conf += 0.3
            glacier_signal = confirmed = True
            rocky_signal = True  # meltwater carried down a valley = rock flour
            a.evidence.append(f"fed by {stream_label or 'a stream'} flowing from {glacier_name}")
            break

    # 5. Rocky glacial terrain: scree, bare rock, moraines, aretes, cliffs
    kinds: Set[str] = set()
    for r in rocky:
        if not geo.boxes_within(lake.box, r.box, s.rocky_km * 1000):
            continue
        if geo.min_distance_m(shore, geo.thin(r.points, 60)) <= s.rocky_km * 1000:
            kinds.add(r.tags.get("natural") or "moraine")
    if kinds:
        conf += 0.2 if "moraine" in kinds else 0.15
        rocky_signal = True
        a.evidence.append("rocky terrain nearby: " + ", ".join(sorted(kinds)))

    # 6. High alpine lake
    if lake.ele is not None and lake.ele >= s.high_lake_m:
        conf += 0.1
        a.evidence.append(f"high-altitude lake ({lake.ele:.0f} m)")
    if lake.ele is None and a.nearest_glacier_km is not None:
        a.evidence.append("elevation unknown (run without --no-elevation)")

    a.confidence = round(min(conf, 1.0), 2)
    # Blue needs an active glacier AND rock for it to grind through.
    candidate = glacier_signal and rocky_signal and a.confidence >= s.blue_threshold
    a.is_blue = candidate and confirmed
    a.is_likely_blue = candidate and not confirmed
    return a


@dataclass
class LakeScore:
    lake: LakeAssessment
    score: float
    peak_points: float
    lake_points: float
    blue_points: float
    peaks: List[Feature]
    nearest_place: Optional[Feature]
    nearest_place_km: Optional[float]
    cover: Cover = field(default_factory=Cover)


# ----------------------------------------------------------------- places ---

def _closeness(d_km: float, radius_km: float) -> float:
    return max(0.0, 1 - d_km / radius_km) * 0.8 + 0.2 if d_km <= radius_km else 0.0


def _peak_points(origin: List[geo.Point], peaks: List[Feature], s: Settings) -> float:
    """Points for the peaks around `origin` (a place's point or a lake's shoreline)."""
    pts = 0.0
    for p in peaks:
        height = s.peak_points + max(0.0, (p.ele or 1000) - 1000) / 1000
        pts += height * _closeness(geo.min_distance_m(origin, [p.center]) / 1000, s.mountain_km)
    # Soft cap: the first few big peaks matter most, a 50th peak adds little.
    return s.max_peak_points * (1 - math.exp(-pts / s.max_peak_points))


class LandCover:
    """Point-in-polygon lookups for green land, barren land and water."""

    def __init__(self, features: Dict[str, List[Feature]], s: Settings):
        self.s = s
        barren = (features.get("barren", []) + features.get("glacier", [])
                  + [r for r in features.get("rocky", []) if r.tags.get("natural") in ("scree", "bare_rock")])
        self.green = geo.PolygonIndex([f.rings for f in features.get("green", []) if f.rings])
        self.barren = geo.PolygonIndex([f.rings for f in barren if f.rings])
        self.water = geo.PolygonIndex([f.rings for f in features.get("lake", []) if f.rings])

    def around(self, center: geo.Point, radius_m: float) -> Cover:
        land = green = barren = 0
        for p in geo.sample_disc(center, radius_m):
            if self.water.contains(p):
                continue
            land += 1
            if self.green.contains(p):
                green += 1
            elif self.barren.contains(p):
                barren += 1
        if not land:
            return Cover()
        g, b = green / land, barren / land
        return Cover(round(g, 2), round(b, 2), round(self.s.green_points * g - self.s.barren_points * b, 1))

    def around_lake(self, lake: Feature) -> Cover:
        reach = max((geo.haversine_m(lake.center, p) for p in geo.thin(lake.points, 60)), default=0)
        return self.around(lake.center, min(reach, 10_000) + self.s.green_km * 1000)


def _blue_share(la: LakeAssessment, s: Settings) -> float:
    return 1.0 if la.is_blue else s.likely_factor if la.is_likely_blue else 0.0


def assess_lakes(features: Dict[str, List[Feature]], s: Settings) -> List[LakeAssessment]:
    lakes = [l for l in features["lake"] if _lake_area_ha(l) >= s.min_lake_ha]
    fed = glacier_fed_streams(features["stream"], features["glacier"], s)
    return [assess_lake(l, features["glacier"], features["rocky"], features["stream"], fed, s) for l in lakes]


def score_lakes(
    features: Dict[str, List[Feature]],
    assessed: List[LakeAssessment],
    s: Optional[Settings] = None,
    require_mountains: bool = True,
    cover: Optional[LandCover] = None,
) -> List[LakeScore]:
    """Scoreboard of lakes: mountains around the shore + the lake itself + blue bonus
    + green surroundings (minus for barren ones)."""
    s = s or Settings()
    cover = cover or LandCover(features, s)
    results: List[LakeScore] = []
    for la in assessed:
        shore = geo.thin(la.lake.points, 100)
        peaks = [p for p in features["peak"]
                 if geo.bbox_distance_m(p.center, la.lake.box) <= s.mountain_km * 1000
                 and geo.min_distance_m(shore, [p.center]) <= s.mountain_km * 1000]
        if require_mountains and not peaks:
            continue
        peak_pts = _peak_points(shore, peaks, s)
        lake_pts = s.lake_points + (s.big_lake_points if la.area_ha > 50 else 0)
        blue_pts = s.blue_lake_points * la.confidence * _blue_share(la, s)

        nearest, nearest_km = None, None
        for place in features["place"]:
            if geo.bbox_distance_m(place.center, la.lake.box) > 20_000:
                continue
            d = geo.min_distance_m([place.center], shore) / 1000
            if nearest_km is None or d < nearest_km:
                nearest, nearest_km = place, round(d, 2)

        land = cover.around_lake(la.lake)
        peaks.sort(key=lambda p: p.ele or 0, reverse=True)
        results.append(LakeScore(la, round(peak_pts + lake_pts + blue_pts + land.points, 1), round(peak_pts, 1),
                                 round(lake_pts, 1), round(blue_pts, 1), peaks, nearest, nearest_km, land))
    results.sort(key=lambda r: r.score, reverse=True)
    return results


def score_places(
    features: Dict[str, List[Feature]],
    s: Optional[Settings] = None,
    require_both: bool = True,
    assessed: Optional[List[LakeAssessment]] = None,
    cover: Optional[LandCover] = None,
) -> tuple:
    """Return (sorted list of PlaceScore, list of LakeAssessment)."""
    s = s or Settings()
    cover = cover or LandCover(features, s)
    if assessed is None:
        assessed = assess_lakes(features, s)

    results: List[PlaceScore] = []
    for place in features["place"]:
        c = place.center
        peaks = [p for p in features["peak"] if geo.haversine_m(c, p.center) <= s.mountain_km * 1000]
        near: List[LakeAssessment] = []
        dists: Dict[int, float] = {}
        for la in assessed:
            if geo.bbox_distance_m(c, la.lake.box) > s.lake_km * 1000:
                continue
            d = geo.min_distance_m([c], geo.thin(la.lake.points, 150))
            if d <= s.lake_km * 1000:
                near.append(la)
                dists[la.lake.osm_id] = round(d / 1000, 2)
        if require_both and (not peaks or not near):
            continue

        def worth(la: LakeAssessment) -> tuple:
            w = _closeness(dists[la.lake.osm_id], s.lake_km)
            base = (s.lake_points + (s.big_lake_points if la.area_ha > 50 else 0)) * w
            blue = s.blue_lake_points * la.confidence * w * _blue_share(la, s)
            return base, blue

        near.sort(key=lambda la: sum(worth(la)), reverse=True)
        counted = [worth(la) for la in near[: s.max_lakes_counted]]
        lake_pts = sum(b for b, _ in counted)
        blue_pts = sum(x for _, x in counted)
        peak_pts = _peak_points([place.center], peaks, s)
        land = cover.around(place.center, s.green_km * 1000)
        peaks.sort(key=lambda p: p.ele or 0, reverse=True)
        results.append(PlaceScore(place, round(peak_pts + lake_pts + blue_pts + land.points, 1),
                                  round(peak_pts, 1), round(lake_pts, 1), round(blue_pts, 1),
                                  peaks, near, dists, land))

    results.sort(key=lambda r: r.score, reverse=True)
    return results, assessed
