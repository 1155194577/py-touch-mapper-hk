import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from shapely import Point

from touch_mapper import hkstp
from touch_mapper.cli import main
from touch_mapper.floor_mesh import FILES, _projector, _read_geometry, _relief
from touch_mapper.print_mesh import _load_shells


def xy(x, y):
    return [114.210 + x * 0.00001, 22.427 + y * 0.00001]


def polygon(west, south, east, north):
    return [[xy(west, south), xy(east, south), xy(east, north), xy(west, north), xy(west, south)]]


def feature(geometry_type, coordinates, **properties):
    return {
        "type": "Feature",
        "properties": {"FLPOLYID": hkstp.FLOOR_ID, **properties},
        "geometry": {"type": geometry_type, "coordinates": coordinates},
    }


def snapshot():
    layers = {
        "floor": [feature(
            "Polygon", polygon(0, 0, 100, 80),
            BLDGCSUID=hkstp.BUILDING_ID, ENGDISFLNM=hkstp.FLOOR, RESTRICTED="N",
        )],
        "units": [
            feature("Polygon", polygon(20, 20, 32, 32), RESTRICTED="N"),
            feature("Polygon", polygon(60, 48, 82, 67), RESTRICTED="Y"),
        ],
        "gates": [feature("LineString", [xy(16, 0), xy(20, 0)], GATE="pedestrian.principal")],
        "network": [
            feature("LineString", [xy(14, 52), xy(95, 52)], FLPOLYID=hkstp.NETWORK_ID,
                    RESTRICTED="N", HIGHWAY="footway"),
            feature("LineString", [xy(10, 40), xy(95, 40)], FLPOLYID=hkstp.NETWORK_ID,
                    RESTRICTED="Y", HIGHWAY="footway"),
        ],
        "landmarks": [
            feature("MultiPoint", [xy(20, 65), xy(26, 65)], PLACE="elevator", NAMEEN="Lift"),
            feature("Point", xy(36, 65), PLACE="guestservices", NAMEEN="Reception"),
            feature("Point", xy(48, 65), PLACE="restroom.wheelchair", NAMEEN="Accessible Toilet"),
            feature("Point", xy(65, 60), PLACE="elevator", NAMEEN="Restricted Lift"),
        ],
    }
    return {"fetchedAt": "2026-06-15T00:00:00+00:00", "layers": layers}


def page(*ids, more=False):
    return io.BytesIO(json.dumps({
        "type": "FeatureCollection",
        "features": [{"properties": {"OBJECTID": item}} for item in ids],
        "exceededTransferLimit": more,
    }).encode())


class HkstpTest(unittest.TestCase):
    def test_query_uses_ordered_keyset_pagination_and_wgs84(self):
        with patch.object(hkstp, "urlopen", side_effect=[page(1, 9, more=True), page(20)]) as request:
            rows = hkstp._query("network")
        self.assertEqual([r["properties"]["OBJECTID"] for r in rows], [1, 9, 20])
        start, end = [parse_qs(urlsplit(call.args[0].full_url).query) for call in request.call_args_list]
        self.assertEqual(start["outSR"], ["4326"])
        self.assertEqual(start["orderByFields"], ["OBJECTID ASC"])
        self.assertIn(hkstp.NETWORK_ID, start["where"][0])
        self.assertTrue(end["where"][0].endswith("OBJECTID>9"))

    def test_query_rejects_oversized_or_replayed_pages(self):
        with patch.object(hkstp, "MAX_PAGE_BYTES", 10), patch.object(hkstp, "urlopen", return_value=page(1)):
            with self.assertRaisesRegex(ValueError, "size limit"):
                hkstp._query("network")
        with patch.object(hkstp, "urlopen", side_effect=[page(8, more=True), page(8)]):
            with self.assertRaisesRegex(ValueError, "not ordered"):
                hkstp._query("network")

    def test_cache_is_reused_and_stale_snapshot_survives_outage(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CACHE_HOME": root}):
            with patch.object(hkstp, "_query", side_effect=snapshot()["layers"].__getitem__) as query:
                live, source = hkstp.load_floor()
                self.assertEqual(source, "live")
                self.assertEqual(hkstp.load_floor(), (live, "cache"))
                self.assertEqual(query.call_count, 5)
            cache = next(Path(root).rglob("*.json"))
            os.utime(cache, (1, 1))
            with (
                patch.object(hkstp, "_query", side_effect=URLError("offline")),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                self.assertEqual(hkstp.load_floor(), (live, "stale-cache"))
            self.assertIn("using cached floor data", stderr.getvalue())

    def test_ambiguous_prefix_and_invalid_coordinate_are_rejected(self):
        data = snapshot()
        data["layers"]["floor"] *= 2
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            hkstp._validate_snapshot(data)
        data = snapshot()
        data["layers"]["landmarks"][1]["geometry"]["coordinates"] = [22.427, 114.210]
        with self.assertRaisesRegex(ValueError, "geometry in WGS84"):
            hkstp._validate_snapshot(data)

    def test_restricted_rooms_routes_and_points_are_not_rendered(self):
        layers = snapshot()["layers"]
        project = _projector(_read_geometry(layers["floor"][0]), 190)
        _, relief, landmarks = _relief(layers, 190)
        self.assertTrue(relief["units"].covers(project(Point(xy(20, 25)))))
        self.assertFalse(relief["units"].covers(project(Point(xy(70, 48)))))
        self.assertTrue(relief["corridors"].covers(project(Point(xy(38, 52)))))
        self.assertFalse(relief["corridors"].covers(project(Point(xy(70, 52)))))
        self.assertFalse(relief["corridors"].covers(project(Point(xy(38, 40)))))
        self.assertNotIn("Restricted Lift", [item.get("name") for item in landmarks])

    def test_explicit_command_exports_one_valid_solid_and_legend_without_runtime(self):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(hkstp, "load_floor", return_value=(snapshot(), "live")),
            patch("touch_mapper.cli.Runtime") as runtime,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            out = Path(root) / "floor"
            self.assertEqual(main(["hkstp-floor", "--building", "Green 18", "--floor", "GF", "-o", str(out)]), 0)
            runtime.assert_not_called()
            self.assertEqual(set(p.name for p in out.iterdir()), set(FILES))
            manifest = json.loads(stdout.getvalue())
            self.assertEqual(manifest, json.loads((out / "manifest.json").read_text()))
            self.assertEqual(manifest["floorId"], hkstp.FLOOR_ID)
            self.assertEqual(max(manifest["printSizeMm"]), 190)
            self.assertEqual(len(manifest["landmarks"]), 5)
            self.assertIn(hkstp.ATTRIBUTION, (out / "legend.md").read_text())
            shells = _load_shells(out / "map.stl")
            self.assertEqual(len(shells), 1)
            self.assertGreater(shells[0].volume(), 0)
            self.assertEqual(shells[0].bounding_box()[2], 0)

    def test_small_print_keeps_restroom_cross_clear_of_restricted_unit(self):
        layers = snapshot()["layers"]
        layers["landmarks"][2]["geometry"]["coordinates"] = xy(58.5, 60)
        _, relief, landmarks = _relief(layers, 120)
        project = _projector(_read_geometry(layers["floor"][0]), 120)
        point = project(Point(xy(58.5, 60)))
        restricted = project(_read_geometry(layers["units"][1]))
        cross = next(part for part in relief["landmarks"].geoms if part.covers(point))
        self.assertIn("restroom.wheelchair", [item["kind"] for item in landmarks])
        self.assertGreater(cross.distance(restricted), 0.15)
        self.assertGreaterEqual(cross.bounds[2] - cross.bounds[0], 2.63)
        self.assertLess(cross.bounds[2] - cross.bounds[0], 4.4)

    def test_osm_command_does_not_fetch_hkstp(self):
        with (
            patch.object(hkstp, "load_floor") as load,
            patch("touch_mapper.cli.Runtime"),
            patch("touch_mapper.cli.generate", return_value={}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["generate", "--osm", "unused.osm", "--runtime", ".", "-o", "unused"]), 0)
            load.assert_not_called()
