"""Offline tests using a tiny synthetic Overpass response.

Layout (roughly 46.60N, 8.00E):
    glacier  ──stream──>  Gletschersee (blue)        peak 3000 m
                           village "Alpdorf"
                           Lowland Pond (no glacier, far away)
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lakefinder import cli, geo, osm, report  # noqa: E402
from lakefinder.scoring import (Settings, assess_lake, assess_lakes, glacier_fed_streams,  # noqa: E402
                                score_lakes, score_places)


def square(lat, lon, d):
    pts = [(lat, lon), (lat, lon + d), (lat + d, lon + d), (lat + d, lon), (lat, lon)]
    return [{"lat": a, "lon": b} for a, b in pts]


def raw_data(lake_name="Gletschersee", with_stream=True, glacier_ele="2900"):
    elements = [
        {"type": "node", "id": 1, "lat": 46.600, "lon": 8.000, "tags": {"place": "village", "name": "Alpdorf"}},
        {"type": "node", "id": 2, "lat": 46.620, "lon": 8.010, "tags": {"natural": "peak", "name": "Horn", "ele": "3000"}},
        # Lake ~ 400 m x 400 m, 1.5 km north of the village
        {"type": "way", "id": 10, "tags": {"natural": "water", "water": "lake", "name": lake_name, "ele": "1900"},
         "nodes": [100, 101, 102, 103, 100], "geometry": square(46.614, 8.000, 0.004)},
        # Glacier 1.2 km north of the lake
        {"type": "way", "id": 20, "tags": {"natural": "glacier", "name": "Testgletscher", "ele": glacier_ele},
         "nodes": [200, 201, 202, 203, 200], "geometry": square(46.629, 8.000, 0.006)},
        # Scree slope next to the lake
        {"type": "way", "id": 30, "tags": {"natural": "scree"},
         "nodes": [300, 301, 302, 303, 300], "geometry": square(46.619, 8.006, 0.002)},
        # A pond far away with nothing glacial around it
        {"type": "way", "id": 40, "tags": {"natural": "water", "water": "lake", "name": "Lowland Pond", "ele": "500"},
         "nodes": [400, 401, 402, 403, 400], "geometry": square(46.560, 7.990, 0.003)},
    ]
    if with_stream:
        # Two stream ways joined at node 501, flowing glacier (north) -> lake (south)
        elements += [
            {"type": "way", "id": 50, "tags": {"waterway": "stream", "name": "Gletscherbach"},
             "nodes": [500, 501], "geometry": [{"lat": 46.6288, "lon": 8.002}, {"lat": 46.624, "lon": 8.002}]},
            {"type": "way", "id": 51, "tags": {"waterway": "stream"},
             "nodes": [501, 502], "geometry": [{"lat": 46.624, "lon": 8.002}, {"lat": 46.6182, "lon": 8.002}]},
        ]
    return {"elements": elements}


class GeoTests(unittest.TestCase):
    def test_haversine_one_degree_latitude(self):
        self.assertAlmostEqual(geo.haversine_m((0, 0), (1, 0)), 111_195, delta=50)

    def test_area_of_square(self):
        ring = [(p["lat"], p["lon"]) for p in square(46.6, 8.0, 0.01)]
        # 0.01 deg lat ≈ 1112 m, 0.01 deg lon at 46.6N ≈ 764 m
        self.assertAlmostEqual(geo.polygon_area_m2(ring), 1112 * 764, delta=5_000)

    def test_stitch_relation_members_out_of_order(self):
        a, b, c, d = (0, 0), (0, 1), (1, 1), (1, 0)
        rings = osm._stitch([[c, d], [a, b], [c, b], [d, a]])  # shuffled and one reversed
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0][0], rings[0][-1])
        self.assertEqual(len(rings[0]), 5)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings()

    def assess(self, data, name):
        f = osm.parse(data)
        fed = glacier_fed_streams(f["stream"], f["glacier"], self.s)
        lake = next(l for l in f["lake"] if l.name == name)
        return assess_lake(lake, f["glacier"], f["rocky"], f["stream"], fed, self.s)

    def test_parse_classifies_features(self):
        f = osm.parse(raw_data())
        self.assertEqual({k: len(v) for k, v in f.items()},
                         {"place": 1, "peak": 1, "lake": 2, "glacier": 1, "rocky": 1, "stream": 2,
                          "green": 0, "barren": 0})

    def test_meltwater_stream_is_traced_downstream(self):
        f = osm.parse(raw_data())
        fed = glacier_fed_streams(f["stream"], f["glacier"], self.s)
        self.assertEqual(set(fed), {0, 1})

    def test_glacier_fed_lake_is_blue(self):
        a = self.assess(raw_data(), "Gletschersee")
        self.assertTrue(a.is_blue)
        self.assertGreaterEqual(a.confidence, 0.8)
        self.assertTrue(any("fed by Gletscherbach" in e for e in a.evidence))

    def test_lake_without_glacier_is_not_blue(self):
        a = self.assess(raw_data(), "Lowland Pond")
        self.assertFalse(a.is_blue or a.is_likely_blue)
        self.assertEqual(a.confidence, 0.0)

    def test_no_traced_stream_is_only_likely_blue(self):
        a = self.assess(raw_data(lake_name="Bergsee", with_stream=False), "Bergsee")
        self.assertFalse(a.is_blue)
        self.assertTrue(a.is_likely_blue)

    def test_glacier_below_lake_does_not_count(self):
        a = self.assess(raw_data(lake_name="Bergsee", with_stream=False, glacier_ele="1700"), "Bergsee")
        self.assertFalse(a.is_blue or a.is_likely_blue)

    def test_blue_lake_earns_bonus_points(self):
        with_blue, _ = score_places(osm.parse(raw_data()), self.s)
        plain, _ = score_places(osm.parse(raw_data(lake_name="Bergsee", with_stream=False,
                                                   glacier_ele="1700")), self.s)
        self.assertEqual(with_blue[0].place.name, "Alpdorf")
        self.assertGreater(with_blue[0].blue_points, 0)
        self.assertEqual(plain[0].blue_points, 0)
        self.assertGreater(with_blue[0].score, plain[0].score)

    def test_place_without_mountains_is_skipped(self):
        data = raw_data()
        data["elements"] = [e for e in data["elements"] if e["tags"].get("natural") != "peak"]
        results, _ = score_places(osm.parse(data), self.s)
        self.assertEqual(results, [])


class LakeScoreboardTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings()

    def board(self, data, require_mountains=True):
        f = osm.parse(data)
        return score_lakes(f, assess_lakes(f, self.s), self.s, require_mountains)

    def test_blue_lake_gets_plus_points_and_ranks_first(self):
        board = self.board(raw_data())
        self.assertEqual(board[0].lake.lake.name, "Gletschersee")
        self.assertGreater(board[0].blue_points, 0)
        self.assertGreater(board[0].peak_points, 0)
        self.assertAlmostEqual(board[0].score, board[0].peak_points + board[0].lake_points
                               + board[0].blue_points, delta=0.2)

    def test_same_lake_scores_lower_without_glacier(self):
        blue = self.board(raw_data(lake_name="Bergsee"))[0]
        plain = self.board(raw_data(lake_name="Bergsee", with_stream=False, glacier_ele="1700"))[0]
        self.assertEqual(plain.blue_points, 0)
        self.assertGreater(blue.score, plain.score)
        self.assertAlmostEqual(blue.peak_points, plain.peak_points)

    def test_lake_without_mountains_is_skipped_unless_all(self):
        # Lowland Pond is ~6.6 km from the only peak; shrink the radius so it has none.
        self.s.mountain_km = 3
        names = [r.lake.lake.name for r in self.board(raw_data())]
        self.assertNotIn("Lowland Pond", names)
        names = [r.lake.lake.name for r in self.board(raw_data(), require_mountains=False)]
        self.assertIn("Lowland Pond", names)

    def test_nearest_place_is_reported(self):
        top = self.board(raw_data())[0]
        self.assertEqual(top.nearest_place.name, "Alpdorf")
        self.assertLess(top.nearest_place_km, 2)


def with_cover(data, tags):
    """Add one big land-cover polygon (~6-9 km) centred on the village."""
    data["elements"].append({"type": "way", "id": 90, "tags": tags, "nodes": [1, 2, 3, 4, 1],
                             "geometry": square(46.56, 7.96, 0.08)})
    return data


class GreenTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings()

    def place(self, data):
        results, _ = score_places(osm.parse(data), self.s)
        return results[0]

    def test_unmapped_land_is_neutral(self):
        r = self.place(raw_data())
        self.assertEqual(r.cover.points, 0)

    def test_forest_around_is_a_plus(self):
        r = self.place(with_cover(raw_data(), {"landuse": "forest"}))
        self.assertGreater(r.cover.green, 0.9)
        self.assertAlmostEqual(r.cover.points, self.s.green_points * r.cover.green, delta=0.1)
        self.assertAlmostEqual(r.score, r.peak_points + r.lake_points + r.blue_points + r.cover.points, delta=0.3)

    def test_barren_around_is_a_minus_but_still_listed(self):
        r = self.place(with_cover(raw_data(), {"natural": "sand"}))
        self.assertGreater(r.cover.barren, 0.9)
        self.assertLess(r.cover.points, 0)

    def test_green_ranks_above_barren(self):
        green = self.place(with_cover(raw_data(), {"natural": "grassland"}))
        barren = self.place(with_cover(raw_data(), {"natural": "bare_rock"}))
        self.assertGreater(green.score, barren.score)

    def test_lake_water_is_not_counted_as_land(self):
        f = osm.parse(with_cover(raw_data(), {"natural": "wood"}))
        board = score_lakes(f, assess_lakes(f, self.s), self.s)
        top = next(r for r in board if r.lake.lake.name == "Gletschersee")
        self.assertGreater(top.cover.green, 0)
        self.assertLessEqual(top.cover.green + top.cover.barren, 1.0)


class CliTests(unittest.TestCase):
    def test_offline_run_writes_json_and_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "raw.json")
            with open(src, "w") as f:
                json.dump(raw_data(), f)
            out, page = os.path.join(tmp, "out.json"), os.path.join(tmp, "map.html")
            self.assertEqual(cli.main(["--from-file", src, "--no-elevation", "--json", out, "--map", page]), 0)
            with open(out) as f:
                result = json.load(f)
            self.assertEqual(result["places"][0]["name"], "Alpdorf")
            self.assertEqual(result["lakes"][0]["name"], "Gletschersee")
            self.assertTrue(result["lakes"][0]["blue"])
            with open(page) as f:
                self.assertIn("Gletschersee", f.read())

    def test_world_map_merges_regions(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "raw.json")
            with open(src, "w") as f:
                json.dump(raw_data(), f)
            out = os.path.join(tmp, "results")
            os.mkdir(out)
            for name in ("alpland", "glacia"):
                cli.main(["--from-file", src, "--no-elevation", "--json", os.path.join(out, f"{name}.json")])
            self.assertEqual(cli.main(["--world", out]), 0)
            with open(os.path.join(out, "world.html")) as f:
                page = f.read()
            data = json.loads(page.split("const DATA = ")[1].split(";\n")[0])
            self.assertEqual(data["regions"], ["Alpland", "Glacia"])
            self.assertEqual({l["region"] for l in data["lakes"]}, {"Alpland", "Glacia"})

    def test_map_escapes_script_breakout(self):
        page = report.to_map([], [])
        self.assertNotIn("</script><", page.split("const DATA")[1].split(";")[0])


if __name__ == "__main__":
    unittest.main()
