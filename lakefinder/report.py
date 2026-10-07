"""Render results as a terminal table, JSON, or a standalone Leaflet map."""
from __future__ import annotations

import json
from typing import List, Optional

from .scoring import LakeAssessment, LakeScore, PlaceScore


def _colour(la: LakeAssessment) -> str:
    return "blue" if la.is_blue else "likely blue" if la.is_likely_blue else ""


def lake_dict(la: LakeAssessment) -> dict:
    return {
        "name": la.lake.name,
        "osm": la.lake.url,
        "lat": round(la.lake.center[0], 5),
        "lon": round(la.lake.center[1], 5),
        "ele": la.lake.ele,
        "area_ha": round(la.area_ha, 1),
        "blue": la.is_blue,
        "likely_blue": la.is_likely_blue,
        "glacial_confidence": la.confidence,
        "nearest_glacier_km": la.nearest_glacier_km,
        "evidence": la.evidence,
    }


def lake_score_dict(r: LakeScore) -> dict:
    return dict(
        lake_dict(r.lake),
        score=r.score,
        points={"mountains": r.peak_points, "lake": r.lake_points, "blue_lake_bonus": r.blue_points},
        peak_count=len(r.peaks),
        peaks=[{"name": p.name, "ele": p.ele} for p in r.peaks[:5]],
        nearest_place=r.nearest_place.name if r.nearest_place else None,
        nearest_place_km=r.nearest_place_km,
    )


def place_dict(r: PlaceScore) -> dict:
    return {
        "name": r.place.name,
        "type": r.place.tags.get("place"),
        "osm": r.place.url,
        "lat": round(r.place.center[0], 5),
        "lon": round(r.place.center[1], 5),
        "score": r.score,
        "points": {"mountains": r.peak_points, "lakes": r.lake_points, "blue_lake_bonus": r.blue_points},
        "peak_count": len(r.peaks),
        "peaks": [{"name": p.name, "ele": p.ele} for p in r.peaks[:5]],
        "lakes": [dict(lake_dict(la), distance_km=r.lake_distances_km[la.lake.osm_id]) for la in r.lakes[:5]],
    }


def to_json(results: List[PlaceScore], lakes: List[LakeAssessment],
            lake_scores: Optional[List[LakeScore]] = None) -> str:
    return json.dumps({"lakes": [lake_score_dict(r) for r in lake_scores or []],
                       "places": [place_dict(r) for r in results]}, indent=2, ensure_ascii=False)


def to_table(results: List[PlaceScore], lakes: List[LakeAssessment], top: int,
             lake_scores: Optional[List[LakeScore]] = None) -> str:
    lines = []
    n_blue = sum(l.is_blue for l in lakes)
    n_likely = sum(l.is_likely_blue for l in lakes)
    lines.append(f"Found {len(lakes)} lakes: {n_blue} glacier-fed (blue), {n_likely} likely blue.\n")

    if lake_scores is not None:
        lines.append("LAKE SCOREBOARD — lakes with mountains around them (blue lakes get bonus points)")
        lines.append(f"{'#':>3}  {'Lake':<26} {'Score':>6}  {'Mtn':>5} {'Lake':>5} {'Blue+':>5}  "
                     f"{'Colour':<12} {'Elev':>6}  Nearest place")
        for i, r in enumerate(lake_scores[:top], 1):
            la = r.lake
            ele = f"{la.lake.ele:.0f} m" if la.lake.ele is not None else "?"
            near = f"{r.nearest_place.name} {r.nearest_place_km} km" if r.nearest_place else "-"
            lines.append(f"{i:>3}  {(la.lake.name or '(unnamed)')[:26]:<26} {r.score:>6.1f}  {r.peak_points:>5.1f} "
                         f"{r.lake_points:>5.1f} {r.blue_points:>5.1f}  {_colour(la):<12} {ele:>6}  {near}")
        if not lake_scores:
            lines.append("  No lakes with mountains nearby in this area.")
        blue = [r.lake for r in lake_scores[:top] if r.lake.is_blue or r.lake.is_likely_blue]
        if blue:
            lines.append("\nWhy they look blue:")
            for la in blue:
                lines.append(f"  {la.lake.name or '(unnamed)'} ({_colour(la)}, {la.confidence:.2f}): "
                             + "; ".join(la.evidence))
        lines.append("")

    lines.append("PLACE SCOREBOARD — towns and villages near mountains and lakes")
    lines.append(f"{'#':>3}  {'Place':<26} {'Score':>6}  {'Mtn':>5} {'Lake':>5} {'Blue+':>5}  Best lake")
    for i, r in enumerate(results[:top], 1):
        best = r.lakes[0]
        tag = f" ({_colour(best)})" if _colour(best) else ""
        lines.append(f"{i:>3}  {r.place.name[:26]:<26} {r.score:>6.1f}  {r.peak_points:>5.1f} "
                     f"{r.lake_points:>5.1f} {r.blue_points:>5.1f}  "
                     f"{best.lake.name or '(unnamed)'}{tag} {r.lake_distances_km[best.lake.osm_id]} km")
    if not results:
        lines.append("  No places with both mountains and lakes nearby in this area.")
    return "\n".join(lines)


MAP_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Mountain Lake Finder</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
 html,body{margin:0;height:100%;font:14px system-ui,sans-serif}
 #map{position:absolute;inset:0 340px 0 0}
 #side{position:absolute;top:0;right:0;bottom:0;width:340px;overflow:auto;padding:12px;box-sizing:border-box;background:#fafafa}
 .tabs{display:flex;gap:6px;margin-bottom:8px} .tabs button{flex:1;padding:6px;border:1px solid #bbb;background:#fff;border-radius:6px;cursor:pointer;font:inherit}
 .tabs button.on{background:#0a7bbd;color:#fff;border-color:#0a7bbd}
 .row{padding:6px 4px;border-bottom:1px solid #ddd;cursor:pointer}
 .row:hover{background:#eef} .score{float:right;font-weight:600} .blue{color:#0a7bbd;font-weight:600}
 .pill{display:inline-block;padding:0 6px;border-radius:8px;font-size:11px;margin-left:4px;background:#2ec4e6;color:#003}
 .pill.likely{background:#cdeef7}
 @media (max-width:700px){#map{inset:0 0 45% 0}#side{top:55%;width:100%}}
</style></head><body><div id="map"></div><div id="side">
<div class="tabs"><button id="tLakes" class="on">Lakes</button><button id="tPlaces">Places</button></div>
<div id="list"></div>
<p style="color:#666">Blue = glacier-fed lake (+ bonus). Light blue = likely blue (half bonus). Grey = other lakes. Red = places.</p></div>
<script>
const DATA = __DATA__;
const map = L.map('map');
L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
  {maxZoom:17, attribution:'&copy; OpenStreetMap contributors, SRTM | &copy; OpenTopoMap'}).addTo(map);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pill = l => l.blue ? '<span class=pill>blue</span>' : l.likely_blue ? '<span class="pill likely">likely blue</span>' : '';
const peaksText = ps => ps.map(k => esc(k.name || '?') + (k.ele ? ' ' + k.ele + 'm' : '')).join(', ');
const bounds = [], lakeMarkers = {}, placeMarkers = [];

DATA.lakes.forEach(l => {
  bounds.push([l.lat, l.lon]);
  const st = l.blue ? ['#0a7bbd', '#2ec4e6', 8] : l.likely_blue ? ['#5aa9d6', '#b5e6f3', 7] : ['#888', '#bbb', 5];
  const sc = l.score != null ? `Score <b>${l.score}</b> (mountains ${l.points.mountains} · lake ${l.points.lake} · blue bonus ${l.points.blue_lake_bonus})<br>` : '';
  lakeMarkers[l.osm] = L.circleMarker([l.lat, l.lon], {radius: st[2], color: st[0], fillColor: st[1], fillOpacity: .85, weight: 2})
   .bindPopup(`<b>${esc(l.name || 'Unnamed lake')}</b>${pill(l)}<br>${sc}
     ${l.ele != null ? Math.round(l.ele) + ' m · ' : ''}${l.area_ha} ha${l.nearest_place ? ' · near ' + esc(l.nearest_place) + ' (' + l.nearest_place_km + ' km)' : ''}<br>
     ${l.peaks && l.peaks.length ? 'Peaks: ' + peaksText(l.peaks) + '<br>' : ''}
     ${l.evidence.length ? '<i>' + l.evidence.map(esc).join('<br>') + '</i><br>' : ''}<a href="${l.osm}" target=_blank>OSM</a>`)
   .addTo(map);
});
const maxScore = Math.max(1, ...DATA.places.map(p => p.score));
DATA.places.forEach(p => {
  bounds.push([p.lat, p.lon]);
  placeMarkers.push(L.circleMarker([p.lat, p.lon], {radius: 4 + 8 * p.score / maxScore, color: '#b22', fillColor: '#e55', fillOpacity: .6})
   .bindPopup(`<b>${esc(p.name)}</b> (${esc(p.type)})<br>Score <b>${p.score}</b><br>
     Mountains ${p.points.mountains} · Lakes ${p.points.lakes} · Blue bonus ${p.points.blue_lake_bonus}<br>
     Peaks: ${peaksText(p.peaks)}<br>
     Lakes: ${p.lakes.map(k => esc(k.name || 'unnamed') + pill(k) + ' ' + k.distance_km + 'km').join(', ')}`)
   .addTo(map));
});

const list = document.getElementById('list');
function row(i, name, score, sub, onclick) {
  const r = document.createElement('div'); r.className = 'row';
  r.innerHTML = `${i + 1}. ${name}<span class=score>${score}</span><br><small>${sub}</small>`;
  r.onclick = onclick; list.appendChild(r);
}
function show(which) {
  document.getElementById('tLakes').className = which === 'lakes' ? 'on' : '';
  document.getElementById('tPlaces').className = which === 'places' ? 'on' : '';
  list.innerHTML = '';
  if (which === 'lakes') {
    const ranked = DATA.lakes.filter(l => l.score != null);
    if (!ranked.length) list.textContent = 'No lakes with mountains nearby.';
    ranked.forEach((l, i) => row(i, esc(l.name || 'Unnamed lake') + pill(l), l.score,
      `${l.peak_count} peaks${l.ele != null ? ' · ' + Math.round(l.ele) + ' m' : ''}${l.nearest_place ? ' · near ' + esc(l.nearest_place) : ''}`,
      () => { map.setView([l.lat, l.lon], 13); lakeMarkers[l.osm].openPopup(); }));
  } else {
    if (!DATA.places.length) list.textContent = 'No places with mountains and lakes nearby.';
    DATA.places.forEach((p, i) => row(i, esc(p.name), p.score,
      `${p.lakes.filter(l => l.blue).length} blue, ${p.lakes.filter(l => l.likely_blue).length} likely blue, ${p.peak_count} peaks`,
      () => { map.setView([p.lat, p.lon], 12); placeMarkers[i].openPopup(); }));
  }
}
document.getElementById('tLakes').onclick = () => show('lakes');
document.getElementById('tPlaces').onclick = () => show('places');
show('lakes');
bounds.length ? map.fitBounds(bounds, {padding: [20, 20]}) : map.setView([46.6, 8.0], 9);
</script></body></html>
"""


def to_map(results: List[PlaceScore], lakes: List[LakeAssessment],
           lake_scores: Optional[List[LakeScore]] = None) -> str:
    scored = {r.lake.lake.osm_id: lake_score_dict(r) for r in lake_scores or []}
    # Ranked lakes first (in scoreboard order), then the rest so they still show on the map.
    all_lakes = list(scored.values()) + [lake_dict(l) for l in lakes if l.lake.osm_id not in scored]
    data = {"places": [place_dict(r) for r in results], "lakes": all_lakes}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return MAP_TEMPLATE.replace("__DATA__", payload)
