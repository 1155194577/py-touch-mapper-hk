import contextlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import numpy as np

from touch_mapper import hk_terrain
from touch_mapper.cli import main
from touch_mapper.hk_overview_mesh import FILES
from touch_mapper.print_mesh import _load_shells


def make_dtm_zip(ncols=40, nrows=30, cellsize=5.0, xll=800000.0, yll=800000.0):
    grid = np.zeros((nrows, ncols), dtype=np.float32)
    # Two land masses (north and south) centered horizontally with a harbour between them.
    for r in range(nrows // 6, (nrows * 2) // 5):
        for c in range(ncols // 6, (ncols * 5) // 6):
            dist = abs(r - nrows * 0.28) + abs(c - ncols * 0.5) * 0.5
            grid[r, c] = max(10.0, 900.0 - dist * 20.0)
    for r in range((nrows * 3) // 5, (nrows * 5) // 6):
        for c in range(ncols // 4, (ncols * 3) // 4):
            dist = abs(r - nrows * 0.72) + abs(c - ncols * 0.5) * 0.5
            grid[r, c] = max(10.0, 550.0 - dist * 20.0)
    lines = [
        f"ncols {ncols}",
        f"nrows {nrows}",
        f"xllcorner {xll}",
        f"yllcorner {yll}",
        f"cellsize {cellsize:g}",
        "NODATA_value -9999",
    ]
    for row in grid:
        lines.append(" ".join(f"{v:g}" for v in row))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(hk_terrain.DTM_MEMBER, "\n".join(lines) + "\n")
    return buf.getvalue()


def make_districts(xll=800000.0, yll=800000.0, width=200.0, height=150.0):
    mid_x = xll + width / 2
    return {
        "fetchedAt": "2026-11-25T00:00:00+00:00",
        "features": [
            {
                "attributes": {
                    "OBJECTID": 1,
                    "AREA_CODE": "W",
                    "NAME_EN": "West Test District",
                    "NAME_TC": "西測試區",
                    "REGION": "NT",
                },
                "geometry": {
                    "rings": [[[xll, yll], [mid_x, yll], [mid_x, yll + height], [xll, yll + height], [xll, yll]]]
                },
            },
            {
                "attributes": {
                    "OBJECTID": 2,
                    "AREA_CODE": "E",
                    "NAME_EN": "East Test District",
                    "NAME_TC": "東測試區",
                    "REGION": "HK",
                },
                "geometry": {
                    "rings": [
                        [[mid_x, yll], [xll + width, yll], [xll + width, yll + height], [mid_x, yll + height], [mid_x, yll]]
                    ]
                },
            },
        ],
    }


class HkOverviewTest(unittest.TestCase):
    def test_parse_dtm_zip_validates_header_and_step(self):
        raw = make_dtm_zip(ncols=40, nrows=30, cellsize=5.0)
        parsed = hk_terrain.parse_dtm_zip(raw, step_m=10)
        self.assertEqual(parsed["step_m"], 10.0)
        self.assertGreater(parsed["max_elevation_m"], 500.0)
        self.assertEqual(parsed["grid"].ndim, 2)
        with self.assertRaisesRegex(ValueError, "multiple"):
            hk_terrain.parse_dtm_zip(raw, step_m=7)

    def test_dtm_cache_is_reused_and_stale_cache_survives_outage(self):
        raw = make_dtm_zip(ncols=40, nrows=30, cellsize=5.0)
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CACHE_HOME": root}):
            with patch.object(hk_terrain, "urlopen", return_value=io.BytesIO(raw)) as req:
                dtm_live, src_live = hk_terrain.load_dtm(step_m=10)
                self.assertEqual(src_live, "live")
                dtm_cached, src_cached = hk_terrain.load_dtm(step_m=10)
                self.assertEqual(src_cached, "cache")
                self.assertEqual(req.call_count, 1)
                self.assertEqual(dtm_live["grid"].shape, dtm_cached["grid"].shape)
            cache = next(Path(root).rglob("hk-dtm-5m.zip"))
            os.utime(cache, (1, 1))
            with (
                patch.object(hk_terrain, "urlopen", side_effect=URLError("offline")),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                _, src_stale = hk_terrain.load_dtm(step_m=10)
            self.assertEqual(src_stale, "stale-cache")
            self.assertIn("using cached DTM", stderr.getvalue())

    def test_districts_unavailable_warns_and_returns_none(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CACHE_HOME": root}):
            with (
                patch.object(hk_terrain, "urlopen", side_effect=URLError("offline")),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                data, source = hk_terrain.load_districts()
            self.assertIsNone(data)
            self.assertEqual(source, "unavailable")
            self.assertIn("continuing without district ridges", stderr.getvalue())

    def test_cli_exports_one_valid_solid_and_legend_without_runtime(self):
        dtm = hk_terrain.parse_dtm_zip(make_dtm_zip(ncols=120, nrows=90, cellsize=5.0), step_m=10)
        districts = make_districts(width=600.0, height=450.0)
        with tempfile.TemporaryDirectory() as root:
            out_dir = Path(root) / "hk-overview"
            stdout = io.StringIO()
            with (
                patch.dict(os.environ, {"TOUCH_MAPPER_RUNTIME": ""}),
                patch.object(hk_terrain, "load_dtm", return_value=(dtm, "cache")),
                patch.object(hk_terrain, "load_districts", return_value=(districts, "cache")),
                contextlib.redirect_stdout(stdout),
            ):
                code = main(["hk-overview", "--size-mm", "200", "--relief-mm", "8", "--step-m", "100", "-o", str(out_dir)])
            self.assertEqual(code, 0)
            manifest = json.loads(stdout.getvalue())
            self.assertEqual(manifest["files"], list(FILES))
            self.assertEqual(manifest["region"], "Hong Kong SAR")
            self.assertAlmostEqual(max(manifest["printSizeMm"][:2]), 200.0, delta=0.2)
            self.assertTrue(manifest["districts"]["included"])
            self.assertEqual(manifest["districts"]["count"], 2)
            for name in FILES:
                self.assertTrue((out_dir / name).is_file())
            (solid,) = _load_shells(out_dir / "map.stl")
            self.assertEqual(len(solid.decompose()), 1)
            self.assertGreater(solid.volume(), 0)
            legend = (out_dir / "legend.md").read_text(encoding="utf-8")
            self.assertIn("Hong Kong — Tactile Terrain Overview", legend)
            self.assertIn("West Test District", legend)
            self.assertIn("North-up orientation cue", legend)

    def test_cli_flags_disable_districts_and_borders_and_reject_bad_args(self):
        dtm = hk_terrain.parse_dtm_zip(make_dtm_zip(ncols=80, nrows=60, cellsize=5.0), step_m=10)
        with tempfile.TemporaryDirectory() as root:
            out_dir = Path(root) / "plain"
            stdout = io.StringIO()
            with (
                patch.object(hk_terrain, "load_dtm", return_value=(dtm, "cache")),
                patch.object(hk_terrain, "load_districts") as fetch_districts,
                contextlib.redirect_stdout(stdout),
            ):
                code = main([
                    "hk-overview", "--size-mm", "150", "--no-districts", "--no-borders", "-o", str(out_dir),
                ])
            self.assertEqual(code, 0)
            fetch_districts.assert_not_called()
            manifest = json.loads(stdout.getvalue())
            self.assertFalse(manifest["districts"]["included"])
            self.assertEqual(manifest["districts"]["source"], "disabled")
            self.assertFalse(manifest["hasBorders"])

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                bad = main(["hk-overview", "--size-mm", "90", "-o", str(Path(root) / "too-small")])
            self.assertEqual(bad, 1)
            self.assertIn("--size-mm", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
