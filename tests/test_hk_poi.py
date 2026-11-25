import tempfile
import unittest
from pathlib import Path

import numpy as np

from manifold3d import Manifold

from touch_mapper import hk_poi
from touch_mapper.osm import Bounds
from touch_mapper.print_mesh import _cap_triangular_holes, _is_wall


OSM = """<?xml version="1.0"?>
<osm version="0.6">
  <node id="1" lat="22.3354" lon="114.1562">
    <tag k="railway" v="station"/><tag k="network" v="港鐵 MTR"/><tag k="name" v="長沙灣 Cheung Sha Wan"/>
  </node>
  <node id="2" lat="22.3300" lon="114.1600"/>
  <node id="3" lat="22.3300" lon="114.1610"/>
  <node id="4" lat="22.3310" lon="114.1610"/>
  <way id="10">
    <nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="2"/>
    <tag k="leisure" v="park"/><tag k="name" v="Sham Shui Po Park"/>
  </way>
  <node id="5" lat="22.3360" lon="114.1570"><tag k="amenity" v="restaurant"/><tag k="name" v="Cafe"/></node>
  <node id="6" lat="22.3360" lon="114.1570"><tag k="amenity" v="library"/></node>
  <node id="7" lat="22.3500" lon="114.1570"><tag k="amenity" v="library"/><tag k="name" v="Far Library"/></node>
</osm>
"""


class PoiTest(unittest.TestCase):
    def test_selects_named_pois_in_bounds_by_category(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.osm"
            path.write_text(OSM, encoding="utf-8")
            features = hk_poi.load(path)
        groups, skipped = hk_poi.select(features, Bounds(114.1505, 22.329, 114.1675, 22.3425))
        self.assertEqual(list(groups), [
            ("station:長沙灣 Cheung Sha Wan", "entrance"), ("park:Sham Shui Po Park", "reception"),
        ])
        lon, lat = groups["park:Sham Shui Po Park", "reception"][0][1]
        self.assertAlmostEqual(lon, 114.1605)
        self.assertAlmostEqual(lat, 22.3302, places=4)
        self.assertEqual(skipped, {"outsideMap": 1})

    def test_legend_names_each_number(self):
        data = hk_poi.report({("station:A", "entrance"): []}, {}, [{
            "number": 1, "building": "A|B", "buildingId": "station:A|B", "kind": "entrance",
            "anchorLonLat": [0, 0], "printXYMm": [1, 2], "shiftMm": 0,
        }])
        self.assertEqual(data["pois"][0]["category"], "station")
        self.assertIn("| 1 | A\\|B | MTR station |", hk_poi.legend(data))
        self.assertIn(hk_poi.ATTRIBUTION, hk_poi.legend(data))


    def test_places_marker_on_ground_roof_or_column_over_roads(self):
        bounds = Bounds(114.15, 22.33, 114.151, 22.331)
        width, height = (span * 1000 / 1000 for span in bounds._span_meters())
        plate = Manifold.cube((width, height, 0.6))
        anchor = ((114.1505, 22.3305),)
        group = {("station:A", "entrance"): [("A", anchor[0])]}
        _, placed = hk_poi.build_relief(group, bounds, 1000, plate, Manifold())
        self.assertEqual(placed[0]["mount"], "ground")
        roads = Manifold.cube((width, height, 1.5)).translate((0, 0, 0.6))
        relief, placed = hk_poi.build_relief(group, bounds, 1000, plate, roads)
        self.assertEqual((placed[0]["mount"], placed[0]["shiftMm"]), ("column", 0.0))
        self.assertGreater(relief.bounding_box()[5], 2.1 + 1)
        towers = plate + Manifold.cube((width, height, 3.5))
        self.assertEqual(hk_poi.build_relief(group, bounds, 1000, towers, Manifold())[1][0]["mount"], "roof")


class WallSeamTest(unittest.TestCase):
    def test_accepts_only_wall_strips_standing_on_the_base(self):
        bent = np.array([(x, y, z) for x, y in ((0, 0), (5, 1), (9, 0)) for z in (0.59, 3.49)])
        self.assertTrue(_is_wall(bent, 0))
        self.assertFalse(_is_wall(bent - (0, 0, 0.59), 0))
        self.assertFalse(_is_wall(np.array([(0, 0, 0.6), (20, 0, 0.6), (20, 30, 0.6)]), 0))


    def test_caps_three_edge_holes_only(self):
        cube = Manifold.cube((1, 1, 1)).to_mesh()
        faces = np.asarray(cube.tri_verts)
        capped = _cap_triangular_holes(faces[1:])
        self.assertEqual(len(capped), len(faces))
        self.assertIsNone(_cap_triangular_holes(faces[2:]))


if __name__ == "__main__":
    unittest.main()
