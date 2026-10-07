"""Command-line entry point.

Examples:
  python -m lakefinder --place "Interlaken, Switzerland" --radius 25
  python -m lakefinder --lat 51.42 --lon -116.18 --radius 30 --map banff.html
  python -m lakefinder --bbox 46.4,7.8,46.8,8.3 --json out.json
  python -m lakefinder --from-file cache.json        # re-score saved Overpass data offline
"""
from __future__ import annotations

import argparse
import json
import sys

from . import geo, osm, report
from .scoring import Settings, assess_lakes, score_lakes, score_places


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lakefinder",
                                 description="Find places with mountains and (glacier-fed, blue) lakes nearby using OpenStreetMap.")
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--place", help="place name to search around (geocoded with Nominatim)")
    where.add_argument("--lat", type=float, help="latitude of search center (use with --lon)")
    where.add_argument("--bbox", help="south,west,north,east")
    where.add_argument("--from-file", help="load a saved Overpass JSON response instead of querying")
    ap.add_argument("--lon", type=float)
    ap.add_argument("--radius", type=float, default=20, help="search radius in km around --place/--lat (default 20)")
    ap.add_argument("--top", type=int, default=15, help="rows to print")
    ap.add_argument("--all", "--all-places", dest="all", action="store_true",
                    help="also list lakes without mountains and places missing mountains or lakes")
    ap.add_argument("--json", help="write full results to this JSON file")
    ap.add_argument("--map", help="write an interactive HTML map to this file")
    ap.add_argument("--no-elevation", action="store_true",
                    help="don't look up missing lake/glacier elevations from the Open-Meteo DEM")
    ap.add_argument("--save-raw", help="save the raw Overpass response (re-use with --from-file)")
    for name, default in vars(Settings()).items():
        ap.add_argument("--" + name.replace("_", "-"), type=type(default), default=default,
                        help=argparse.SUPPRESS if name.endswith("points") else None)
    args = ap.parse_args(argv)

    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            raw = json.load(f)
    else:
        if args.bbox:
            box = tuple(float(x) for x in args.bbox.split(","))
            if len(box) != 4:
                ap.error("--bbox needs 4 numbers: south,west,north,east")
        else:
            if args.place:
                print(f"Geocoding {args.place!r}...", file=sys.stderr)
                center = osm.geocode(args.place)
            else:
                if args.lon is None:
                    ap.error("--lat requires --lon")
                center = (args.lat, args.lon)
            box = geo.bbox_around(center, args.radius * 1000)
        print(f"Querying OpenStreetMap for bbox {tuple(round(x, 4) for x in box)} (this can take a minute)...",
              file=sys.stderr)
        try:
            raw = osm.overpass(osm.build_query(box))
        except RuntimeError as err:
            print(f"{err}\nThe public Overpass servers may be busy; wait a minute or try a smaller --radius.",
                  file=sys.stderr)
            return 1
        if args.save_raw:
            with open(args.save_raw, "w", encoding="utf-8") as f:
                json.dump(raw, f)

    features = osm.parse(raw)
    print("Loaded " + ", ".join(f"{len(v)} {k}s" for k, v in features.items()), file=sys.stderr)

    if not args.no_elevation:
        n = osm.fill_elevations(features["lake"] + features["glacier"])
        print(f"Estimated elevation for {n} lakes/glaciers from terrain data", file=sys.stderr)

    settings = Settings(**{k: getattr(args, k) for k in vars(Settings())})
    lakes = assess_lakes(features, settings)
    lake_scores = score_lakes(features, lakes, settings, require_mountains=not args.all)
    results, _ = score_places(features, settings, require_both=not args.all, assessed=lakes)

    print(report.to_table(results, lakes, args.top, lake_scores))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(report.to_json(results, lakes, lake_scores))
        print(f"\nWrote {args.json}", file=sys.stderr)
    if args.map:
        with open(args.map, "w", encoding="utf-8") as f:
            f.write(report.to_map(results, lakes, lake_scores))
        print(f"Wrote {args.map}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
