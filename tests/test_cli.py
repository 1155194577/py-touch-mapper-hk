import contextlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from xml.etree.ElementTree import parse

from defusedxml.common import DTDForbidden

from touch_mapper.cli import ARTIFACTS, main
from touch_mapper.osm import Bounds, copy_osm, fetch_osm, read_bounds

FIXTURE = Path(__file__).resolve().parent / "fixtures/mixed.osm"


def stl_vertices(path):
    data = path.read_bytes()
    triangles, = struct.unpack_from("<I", data, 80)
    if len(data) != 84 + 50 * triangles:
        raise ValueError("invalid binary STL")
    return [
        vertex
        for offset in range(84, len(data), 50)
        for vertex in (struct.unpack_from("<3f", data, offset + i) for i in (12, 24, 36))
    ]


class OsmTest(unittest.TestCase):
    def test_center_dimensions_and_marker(self):
        bounds = Bounds.centered(60, 24, 18, 12, 3100)
        self.assertEqual(bounds.print_dimensions(3100), (18, 12))
        self.assertEqual(bounds.marker(60, 24), {"x": 0.5, "y": 0.5})
        with self.assertRaisesRegex(ValueError, "4%"):
            bounds.marker(bounds.south, bounds.west)

    def test_reject_reversed_and_nonfinite_bounds(self):
        for values in [(-70, 40, -71, 41), (170, 20, -170, 21), (0, 80, 1, 89), (0, 0, float("nan"), 1)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Bounds(*values)

    def test_read_osm_and_block_dtd(self):
        bounds = read_bounds(FIXTURE)
        self.assertEqual(bounds.print_dimensions(2100), (11.3, 11.32))
        with tempfile.TemporaryDirectory() as root:
            xml = Path(root) / "entity.osm"
            xml.write_text('<!DOCTYPE osm [<!ELEMENT osm ANY>]><osm version="0.6" />')
            with self.assertRaises(DTDForbidden):
                read_bounds(xml)

    def test_reject_edited_or_unbounded_osm(self):
        with tempfile.TemporaryDirectory() as root:
            osm = Path(root) / "edited.osm"
            osm.write_text('<osm version="0.6"><node id="1" action="delete" /></osm>')
            with self.assertRaisesRegex(ValueError, "unedited"):
                read_bounds(osm)
            osm.write_text('<osm version="0.6"><node id="1" version="1" /></osm>')
            with self.assertRaisesRegex(ValueError, "<bounds>"):
                read_bounds(osm)

    def test_download_uses_single_bounded_osm_request(self):
        bounds = Bounds(23.9, 59.9, 24.1, 60.1)
        with tempfile.TemporaryDirectory() as root:
            dest = Path(root) / "download.osm"
            with patch("urllib.request.urlopen", return_value=io.BytesIO(b"osm extract")) as download:
                fetch_osm(bounds, dest, "https://example.com/cli")
            request = download.call_args.args[0]
            self.assertEqual(
                request.full_url, "https://api.openstreetmap.org/api/0.6/map?bbox=23.9,59.9,24.1,60.1"
            )
            self.assertIn("https://example.com/cli", request.get_header("User-agent"))
            self.assertEqual(dest.read_bytes(), b"osm extract")
            with patch("urllib.request.urlopen") as download:
                with self.assertRaisesRegex(ValueError, "0.25 square degrees"):
                    fetch_osm(Bounds(23, 59, 24, 60), dest, None)
                download.assert_not_called()

    def test_download_handles_rejected_area_and_oversize_data(self):
        with tempfile.TemporaryDirectory() as root:
            dest = Path(root) / "download.osm"
            error = urllib.error.HTTPError("https://example.com", 400, "Bad Request", None, None)
            with patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaisesRegex(ValueError, "smaller bounding box"):
                    fetch_osm(Bounds(23.9, 59.9, 24.1, 60.1), dest, None)
            with patch("touch_mapper.osm.MAX_OSM_BYTES", 4):
                with self.assertRaisesRegex(ValueError, "smaller area"):
                    copy_osm(io.BytesIO(b"too much"), dest)


class CliTest(unittest.TestCase):
    def test_clean_error_for_incompatible_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["generate", "--osm", str(FIXTURE), "--width-cm", "10", "-o", "map"]), 1)
        self.assertIn("require --center", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_incomplete_render_does_not_publish_a_partial_map(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "map"
            with patch("touch_mapper.cli.Runtime"), contextlib.redirect_stderr(io.StringIO()) as stderr:
                status = main(["generate", "--runtime", root, "--osm", str(FIXTURE), "-o", str(output)])
            self.assertEqual(status, 1)
            self.assertIn("did not produce map.stl", stderr.getvalue())
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_existing_output_is_not_modified(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "existing"
            output.mkdir()
            (output / "user.txt").write_text("keep")
            with patch("touch_mapper.cli.Runtime"), contextlib.redirect_stderr(io.StringIO()) as stderr:
                status = main(["generate", "--runtime", root, "--osm", str(FIXTURE), "-o", str(output)])
            self.assertEqual(status, 1)
            self.assertIn("output directory already exists", stderr.getvalue())
            self.assertEqual((output / "user.txt").read_text(), "keep")


@unittest.skipUnless(os.environ.get("TOUCH_MAPPER_TEST_ENGINE") == "1", "requires the built Touch Mapper engine")
class RealEngineTest(unittest.TestCase):
    def render(self, input_path, output, *options):
        process = subprocess.run(
            [sys.executable, "-m", "touch_mapper", "generate", "--osm", str(input_path),
             "--scale", "2100", "--out-dir", str(output), *options],
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def test_offline_artifacts_match_print_dimensions_and_feature_description(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "rectangular map"
            manifest = self.render(FIXTURE, output, "--marker", "60.001", "24.002")
            self.assertEqual(json.loads((output / "manifest.json").read_text()), manifest)
            self.assertEqual({path.name for path in output.iterdir()}, set(ARTIFACTS) | {"manifest.json"})
            full = stl_vertices(output / "map.stl")
            ways = stl_vertices(output / "map-ways.stl")
            rest = stl_vertices(output / "map-rest.stl")
            self.assertTrue(ways and rest)
            self.assertCountEqual(full, ways + rest)
            for axis, key in enumerate(["printWidthCm", "printHeightCm"]):
                span = max(vertex[axis] for vertex in full) - min(vertex[axis] for vertex in full)
                self.assertAlmostEqual(span, manifest[key] * 10, delta=0.1)
            svg = parse(output / "map.svg").getroot()
            self.assertTrue(svg.tag.endswith("}svg"))
            self.assertTrue((output / "map.pdf").read_bytes().startswith(b"%PDF"))
            content = json.loads((output / "map-content.json").read_text())
            self.assertIn("Main Street", json.dumps(content["A"]))
            self.assertIn("Canal Walk", json.dumps(content["A"]))
            self.assertIn("Library", json.dumps(content["C"]))
            self.assertEqual(content["metadata"]["printHeightsMm"]["buildings"], 2.9)

    def test_empty_map_and_filtered_buildings(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            empty = root / "empty.osm"
            empty.write_text('<osm version="0.6"><bounds minlon="24" minlat="60" maxlon="24.001" maxlat="60.001" /></osm>')
            self.render(empty, root / "empty")
            self.assertTrue(stl_vertices(root / "empty/map.stl"))
            self.assertFalse(stl_vertices(root / "empty/map-ways.stl"))
            self.render(FIXTURE, root / "filtered", "--content-mode", "only-named-roads", "--building-height-mm", "0")
            filtered = json.loads((root / "filtered/map-content.json").read_text())
            self.assertNotIn("Library", json.dumps(filtered))
            self.assertIn("Main Street", json.dumps(filtered["A"]))
            self.assertEqual(filtered["metadata"]["printHeightsMm"]["buildings"], 0)
