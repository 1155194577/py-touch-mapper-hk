import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

from manifold3d import Manifold

from touch_mapper import hk_paving
from touch_mapper.cli import main
from touch_mapper.osm import Bounds
from touch_mapper.print_mesh import _load_shells

BOUNDS = Bounds(114.2, 22.4, 114.201, 22.401)
LINES = [
    [(114.199, 22.4005), (114.202, 22.4005)],
    [(114.198, 22.402), (114.202, 22.402)],
]


def guide_archive():
    data = io.BytesIO()
    lines = "".join(
        "<LineString><coordinates>" + " ".join(f"{lon},{lat},0" for lon, lat in line) + "</coordinates></LineString>"
        for line in LINES
    )
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "doc.kml",
            f'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><MultiGeometry>{lines}</MultiGeometry>'
            '</Placemark></kml>',
        )
    return data.getvalue()


class HkPavingTest(unittest.TestCase):
    def test_kmz_download_is_cached_and_stale_cache_survives_an_outage(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CACHE_HOME": root}):
            with patch.object(hk_paving, "urlopen", return_value=io.BytesIO(guide_archive())) as request:
                self.assertEqual(hk_paving.load_paths(BOUNDS), LINES)
                self.assertEqual(request.call_args.args[0].full_url, hk_paving.DOWNLOAD)
                self.assertEqual(hk_paving.load_paths(BOUNDS), LINES)
                request.assert_called_once()

            cache = next(Path(root).rglob("*.kmz"))
            os.utime(cache, (1, 1))
            with patch.object(hk_paving, "urlopen", side_effect=urllib.error.URLError("offline")), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(hk_paving.load_paths(BOUNDS), LINES)
            self.assertIn("using cached data", stderr.getvalue())

    def test_bad_kmz_warns_and_has_no_cache(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CACHE_HOME": root}), \
                patch.object(hk_paving, "urlopen", return_value=io.BytesIO(b"not a zip")), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertIsNone(hk_paving.load_paths(BOUNDS))
            self.assertIn("continuing without the layer", stderr.getvalue())
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_non_hk_bounds_do_not_download(self):
        with patch.object(hk_paving, "urlopen") as request:
            self.assertEqual(hk_paving.load_paths(Bounds(24, 60, 24.1, 60.1)), [])
            request.assert_not_called()

    def test_clips_crossing_path_even_without_an_in_bounds_vertex(self):
        relief, count = hk_paving.paving_relief(LINES, BOUNDS, 1000, Manifold(), Manifold())
        self.assertEqual(len(relief.decompose()), count)
        self.assertGreater(count, 1)
        west, south, bottom, east, north, top = relief.bounding_box()
        self.assertGreater(west, 0)
        self.assertLess(east, BOUNDS.print_dimensions(1000)[0] * 10)
        self.assertAlmostEqual((south + north) / 2, BOUNDS.print_dimensions(1000)[1] * 5, delta=0.05)
        self.assertAlmostEqual(north - south, 2 * hk_paving.DOT_RADIUS_MM)
        self.assertAlmostEqual(bottom, hk_paving.DOT_BOTTOM_MM)
        self.assertAlmostEqual(top, hk_paving.DOT_TOP_MM)

    def test_dot_just_above_target_spacing_stays_centered(self):
        length_mm = hk_paving.DOT_SPACING_MM + 0.03
        self.assertEqual(list(hk_paving._dot_centers([(0, 0), (length_mm, 0)])), [(length_mm / 2, 0)])

    def test_dots_do_not_fuse_at_a_hairpin(self):
        run = [(0, 0), (9, 0), (9, 1), (0, 1)]
        candidates = list(hk_paving._dot_centers(run))
        dots = list(hk_paving._separate_centers(candidates))
        self.assertGreater(len(dots), 1)
        self.assertLess(len(dots), len(candidates))
        for i, (x, y) in enumerate(dots):
            for a, b in dots[:i]:
                self.assertGreater((x - a) ** 2 + (y - b) ** 2, (2 * hk_paving.DOT_RADIUS_MM) ** 2)

    def test_buried_candidate_does_not_displace_adjacent_visible_dot(self):
        building = Manifold.cube((4, 3, 3)).translate((0, 3, 0.5))
        relief, count = hk_paving._build_relief([(3, 4.5), (4.9, 4.5)], building, Manifold())
        self.assertEqual(count, 1)
        self.assertAlmostEqual(relief.bounding_box()[0], 4.9 - hk_paving.DOT_RADIUS_MM)

    def test_corner_only_path_is_free_of_marker_and_border_conflicts(self):
        paths = [[(114.20097, 22.400975), (114.20098, 22.40098)]]
        relief, count = hk_paving.paving_relief(paths, BOUNDS, 1000, Manifold(), Manifold(), has_borders=True)
        self.assertEqual(count, 0)
        self.assertTrue(relief.is_empty())
        relief, count = hk_paving.paving_relief(paths, BOUNDS, 1000, Manifold(), Manifold(), has_borders=False)
        self.assertEqual(count, 1)
        self.assertFalse(relief.is_empty())

    def test_short_path_crossing_marker_gets_a_dot_in_its_visible_run(self):
        paths = [[(114.200954, 22.400974), (114.20098, 22.400974)]]
        unclipped, = hk_paving._paving_centers(paths, BOUNDS, 1000, False)
        clipped, = hk_paving._paving_centers(paths, BOUNDS, 1000, True)
        corner_x = BOUNDS._span_meters()[0] - 3 * hk_paving.BORDER_WIDTH_MM - hk_paving.DOT_RADIUS_MM
        self.assertGreater(unclipped[0], corner_x)
        self.assertLess(clipped[0], corner_x)
        self.assertEqual(clipped[1], unclipped[1])

    def test_marker_clearance_splits_a_crossing_into_two_visible_runs(self):
        margin = hk_paving.BORDER_WIDTH_MM + hk_paving.DOT_RADIUS_MM
        corner = 3 * hk_paving.BORDER_WIDTH_MM + hk_paving.DOT_RADIUS_MM
        runs = list(hk_paving._runs([(24, 27.9), (27.9, 24)], 30, 30, margin, corner))
        self.assertEqual(len(runs), 2)
        left, bottom = [next(hk_paving._dot_centers(run)) for run in runs]
        self.assertLess(left[0], 30 - corner)
        self.assertLess(bottom[1], 30 - corner)

    @unittest.skipUnless(os.environ.get("TOUCH_MAPPER_TEST_ENGINE") == "1", "requires the built Touch Mapper engine")
    def test_cli_opt_in_adds_attached_relief_and_handles_failure(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CACHE_HOME": root}):
            work = Path(root)
            osm = work / "small.osm"
            osm.write_text(
                '<osm version="0.6"><bounds minlon="114.2" minlat="22.4" maxlon="114.201" maxlat="22.401"/></osm>'
            )
            args = ["generate", "--osm", str(osm), "--no-borders", "--scale", "1000"]
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                with patch.object(hk_paving, "urlopen", side_effect=urllib.error.URLError("offline")) as request:
                    self.assertEqual(main([*args, "-o", str(work / "off")]), 0, stderr.getvalue())
                    request.assert_not_called()
                    self.assertEqual(
                        main([*args, "--hk-tactile-paving", "-o", str(work / "unavailable")]), 0, stderr.getvalue()
                    )
                with patch.object(hk_paving, "urlopen", return_value=io.BytesIO(guide_archive())):
                    self.assertEqual(
                        main([*args, "--hk-tactile-paving", "-o", str(work / "on")]), 0, stderr.getvalue()
                    )

            def manifest(name):
                return json.loads((work / name / "manifest.json").read_text())

            self.assertNotIn("hkTactilePaving", manifest("off"))
            self.assertEqual(manifest("unavailable")["hkTactilePaving"]["status"], "unavailable")
            self.assertEqual((work / "off/map.stl").read_bytes(), (work / "unavailable/map.stl").read_bytes())
            layer = manifest("on")["hkTactilePaving"]
            self.assertEqual(layer["status"], "included")
            self.assertEqual(layer["source"], hk_paving.SOURCE)
            self.assertGreater(layer["dotCount"], 1)
            off, = _load_shells(work / "off/map.stl")
            on, = _load_shells(work / "on/map.stl")
            rest, = _load_shells(work / "on/map-rest.stl")
            self.assertGreater(on.volume(), off.volume())
            self.assertEqual(on.bounding_box()[2], 0)
            self.assertAlmostEqual(on.bounding_box()[5], hk_paving.DOT_TOP_MM, places=5)
            self.assertAlmostEqual(on.volume(), rest.volume())
