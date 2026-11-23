import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from manifold3d import Manifold

from touch_mapper import hkstp, hkstp_landmarks
from touch_mapper.cli import generate, parser
from touch_mapper.osm import Bounds
from touch_mapper.print_mesh import _load_shells, _solid_facets, _write_stl


BOUNDS = Bounds(114.207, 22.423, 114.209, 22.425)
SCALE = 1500


def rectangle(west, south, east, north):
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


def row(kind=None, coordinates=None, **props):
    feature = {"properties": props}
    if kind:
        feature["geometry"] = {"type": kind, "coordinates": coordinates}
    return feature


def snapshot():
    gate = row("LineString", [[114.208, 22.424], [114.208, 22.42403]],
               FLPOLYID="F1", UNPOLYID="U1", GATE="pedestrian.principal")
    return {"fetchedAt": "2026-06-15", "layers": {
        "floor": [row("Polygon", rectangle(114.208, 22.4236, 114.2087, 22.4245),
                      FLPOLYID="F1", BLDGCSUID="B1", ENGDISFLNM="GF", RESTRICTED="N")],
        "gates": [gate, gate, row("LineString", gate["geometry"]["coordinates"],
                                 FLPOLYID="F1", UNPOLYID="U2", GATE="pedestrian.principal")],
        "landmarks": [
            row("Point", [114.2083, 22.42365], FLPOLYID="F1", UNPOLYID="missing", PLACE="guestservices"),
            row("Point", [114.2085, 22.4244], FLPOLYID="F1", UNPOLYID="missing", PLACE="guestservices"),
            row("Point", [114.2083, 22.42365], FLPOLYID="F2", UNPOLYID="missing", PLACE="guestservices"),
        ],
        "units": [row(FLPOLYID="F1", UNPOLYID="U1", RESTRICTED="N"),
                  row(FLPOLYID="F1", UNPOLYID="U2", RESTRICTED="Y")],
        "restricted": [row("Polygon", rectangle(114.2084, 22.4243, 114.2086, 22.4245),
                           FLPOLYID="F1", UNPOLYID="U2", RESTRICTED="Y")],
        "buildings": [row(BuildingCSUID="B1", BuildingNameEN="Visitor Centre")],
    }}


class CampusLandmarkTest(unittest.TestCase):
    def test_campus_cache_uses_snapshot_and_falls_back_when_offline(self):
        data = snapshot()
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CACHE_HOME": root}):
            with patch.object(hkstp, "_fetch_campus", return_value=data["layers"]) as fetch:
                live, source = hkstp.load_campus()
                self.assertEqual(source, "live")
                self.assertEqual(hkstp.load_campus(), (live, "cache"))
                fetch.assert_called_once()
            os.utime(next(Path(root).rglob("*.json")), (1, 1))
            with (
                patch.object(hkstp, "_fetch_campus", side_effect=OSError("offline")),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(hkstp.load_campus(), (live, "stale-cache"))

    def test_building_join_uses_only_ground_level_and_unrestricted_candidates(self):
        groups, skipped = hkstp_landmarks.select(snapshot()["layers"], BOUNDS)
        self.assertEqual(set(groups), {("B1", "entrance"), ("B1", "reception")})
        self.assertEqual(len(groups["B1", "entrance"]), 2)
        reception, = groups["B1", "reception"]
        self.assertEqual(reception[0], "Visitor Centre")
        self.assertAlmostEqual(reception[1][1], 22.4236)
        self.assertEqual(skipped, {"restrictedOrUnknownAccess": 2, "notConfirmedGroundFloor": 1})

    def test_reception_crop_uses_exterior_anchor_not_indoor_desk(self):
        layers = snapshot()["layers"]
        layers["gates"] = []
        layers["landmarks"] = layers["landmarks"][:1]

        anchor_crop = Bounds(114.207, 22.423, 114.209, 22.423625)
        groups, skipped = hkstp_landmarks.select(layers, anchor_crop)
        self.assertEqual(groups, {("B1", "reception"): [("Visitor Centre", (114.2083, 22.4236))]})
        self.assertFalse(skipped)

        desk_crop = Bounds(114.207, 22.423625, 114.209, 22.425)
        groups, skipped = hkstp_landmarks.select(layers, desk_crop)
        self.assertFalse(groups)
        self.assertEqual(skipped, {"outsideMap": 1})

    def test_raised_numbers_clear_roads_and_building_overhangs_and_stay_attached(self):
        groups, _ = hkstp_landmarks.select(snapshot()["layers"], BOUNDS)
        width, height = (cm * 10 for cm in BOUNDS.print_dimensions(SCALE))
        base = Manifold.cube((width, height, 0.6))
        ways = Manifold.cube((6, 55, 1.2)).translate((width / 2 - 3, height / 2 - 28, 0.55))
        roof = Manifold.cube((8, 6, 0.75)).translate((width * 0.65 - 4, height * 0.3 - 3, 0.9))
        footing = Manifold.cube((1, 1, 0.6)).translate((width * 0.65 + 2, height * 0.3 + 1, 0.55))
        rest = base + roof + footing
        overlay, placed = hkstp_landmarks.build_relief(groups, BOUNDS, SCALE, rest, ways)
        self.assertEqual([i["number"] for i in placed], [1, 2])
        self.assertGreater(placed[0]["shiftMm"], 0)
        self.assertTrue((overlay ^ ways).is_empty())
        self.assertTrue((overlay ^ roof).is_empty())
        self.assertGreater(overlay.slice(2).area(), 0)
        self.assertGreater(len(_solid_facets("map.stl", rest + overlay + ways, single_piece=True)), 0)

    def test_generate_is_opt_in_and_writes_matching_legend_and_stl(self):
        with tempfile.TemporaryDirectory() as root, patch.object(hkstp, "load_campus") as fetch:
            root = Path(root)
            osm = root / "input.osm"
            osm.write_text('<osm version="0.6"><bounds minlon="114.207" minlat="22.423" '
                           'maxlon="114.209" maxlat="22.425"/></osm>')
            fetch.return_value = snapshot(), "live"
            runtime = Mock()

            def convert(osm_path, dimensions, *_):
                parent = osm_path.parent
                base = Manifold.cube((10 * dimensions[0], 10 * dimensions[1], 0.6))
                for model, solid in (("map", base), ("map-rest", base), ("map-ways", Manifold())):
                    _write_stl(parent / f"{model}.stl", _solid_facets(model, solid))
                for name in ("map.blend", "map.svg", "map.pdf", "map-content.json"):
                    (parent / name).write_text("test artifact")

            runtime.convert.side_effect = convert
            args = ["generate", "--osm", str(osm), "--scale", str(SCALE)]
            default = generate(runtime, parser().parse_args([*args, "-o", str(root / "plain")]))
            fetch.assert_not_called()
            self.assertNotIn("hkstpLandmarks", default)
            self.assertNotIn("hkstp-landmarks.md", default["files"])
            out = root / "marked"
            result = generate(runtime, parser().parse_args([*args, "--hkstp-landmarks", "-o", str(out)]))
            fetch.assert_called_once()
            self.assertEqual(result["hkstpLandmarks"]["placedCount"], 2)
            self.assertEqual(result["hkstpLandmarks"]["skipped"]["clustered"], 1)
            self.assertIn("Visitor Centre", (out / "hkstp-landmarks.md").read_text())
            self.assertIn(hkstp.ATTRIBUTION, (out / "hkstp-landmarks.md").read_text())
            self.assertGreater(_load_shells(out / "map.stl")[0].bounding_box()[5], 2)
