import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from manifold3d import Manifold, Mesh64, OpType

from touch_mapper.print_mesh import _export_facets, _join_relief, _load_shells, _mesh, _stl_facets, finalize_stls


def raw_stl(path, *solids):
    triangles = []
    for solid in solids:
        mesh = solid.to_mesh64()
        triangles.extend(mesh.vert_properties[mesh.tri_verts])
    with path.open("wb") as stl:
        stl.write(struct.pack("<80sI", b"unprocessed test STL", len(triangles)))
        for triangle in triangles:
            stl.write(struct.pack("<12fH", 0, 0, 0, *triangle.flatten(), 0))


class PrintMeshTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.directory = Path(root.name)
        self.full = self.directory / "map.stl"
        self.rest = self.directory / "map-rest.stl"
        self.ways = self.directory / "map-ways.stl"

    def test_additional_relief_counts_only_exposed_dots(self):
        plate = Manifold.cube((10, 10, 0.6))
        building = Manifold.cube((3, 3, 3)).translate((1, 1, 0.5))
        raw_stl(self.rest, plate, building)
        raw_stl(self.ways)
        dot = Manifold.cylinder(2.6, 0.85, circular_segments=12)
        dots = Manifold.compose([dot.translate((x, x, 0.2)) for x in (2, 6)])
        self.assertEqual(finalize_stls(self.directory, additional_relief=dots), 1)
        full, = _load_shells(self.full)
        self.assertAlmostEqual(full.volume(), 60 + 3 * 3 * 2.9 + 2.2 * 3 * 0.85 ** 2, places=4)

    def test_vector_repair_keeps_exposed_paving_in_full_and_rest_stls(self):
        raw_stl(self.rest, Manifold.cube((10, 10, 0.6)))
        raw_stl(self.ways, Manifold.cube((2, 8, 1.2)).translate((4, 1, 0.5)))
        dot = Manifold.cylinder(2.6, 0.85, circular_segments=12).translate((5, 5, 0.2))
        with patch("touch_mapper.print_mesh._solid_facets", side_effect=ValueError("unsafe STL")):
            self.assertEqual(finalize_stls(self.directory, blender_relief=True, additional_relief=dot), 1)
        full, = _load_shells(self.full)
        rest, = _load_shells(self.rest)
        for solid in (full, rest):
            self.assertEqual(solid.bounding_box()[2], 0)
            self.assertAlmostEqual(solid.bounding_box()[5], 2.8, delta=0.0001)
        ways = _load_shells(self.ways)
        self.assertAlmostEqual(rest.volume() + sum(part.volume() for part in ways), full.volume(), places=4)

    def test_fuses_overlapping_shells_and_partitions_split_stls(self):
        base = Manifold.cube((20, 30, 0.6))
        building = Manifold.cube((3, 4, 2)).translate((2, 2, 0.59))
        road = Manifold.cube((16, 2, 0.8)).translate((1, 3, 0.59))
        raw_stl(self.rest, base, building)
        raw_stl(self.ways, road)
        self.assertEqual(len(_load_shells(self.rest)), 2)

        finalize_stls(self.directory)

        full, = _load_shells(self.full)
        rest, = _load_shells(self.rest)
        ways = _load_shells(self.ways)
        self.assertEqual(full.bounding_box()[:3], (0, 0, 0))
        np.testing.assert_allclose(full.bounding_box()[3:], (20, 30, 2.59))
        self.assertAlmostEqual(full.volume(), (base + building + road).volume(), places=4)
        self.assertAlmostEqual(sum(part.volume() for part in ways) + rest.volume(), full.volume(), places=4)
        self.assertTrue(all((rest ^ part).volume() < 1e-5 for part in ways))

    def test_empty_ways_stl_is_preserved(self):
        raw_stl(self.rest, Manifold.cube((20, 30, 0.6)))
        raw_stl(self.ways)
        finalize_stls(self.directory)
        self.assertEqual(len(_load_shells(self.full)), 1)
        self.assertEqual(_load_shells(self.ways), [])
        self.assertEqual(self.ways.stat().st_size, 84)

    def test_rejects_floating_relief(self):
        raw_stl(self.rest, Manifold.cube((20, 30, 0.6)))
        raw_stl(self.ways, Manifold.cube((1, 2, 1)).translate((4, 5, 2)))
        with self.assertRaisesRegex(ValueError, "not attached to the base"):
            finalize_stls(self.directory)
        self.assertFalse(self.full.exists())

    def test_rejects_open_raw_stl(self):
        raw_stl(self.rest, Manifold.cube((20, 30, 0.6)))
        data = self.rest.read_bytes()
        self.rest.write_bytes(data[:80] + struct.pack("<I", 11) + data[84:-50])
        raw_stl(self.ways)
        with self.assertRaisesRegex(ValueError, "open or invalid surface"):
            finalize_stls(self.directory)
        self.assertFalse(self.full.exists())

    def test_closes_small_seam_in_building_wall(self):
        base = Manifold.cube((20, 30, 0.6))
        building = Manifold.cube((3, 4, 2)).translate((2, 2, 0.59))
        raw_stl(self.rest, base, building)
        data = self.rest.read_bytes()
        self.rest.write_bytes(data[:80] + struct.pack("<I", 23) + data[84:-50])
        raw_stl(self.ways)
        with self.assertRaisesRegex(ValueError, "open or invalid surface"):
            _load_shells(self.rest)

        finalize_stls(self.directory)

        full, = _load_shells(self.full)
        self.assertAlmostEqual(full.volume(), (base + building).volume(), places=4)
        np.testing.assert_allclose(full.bounding_box(), (0, 0, 0, 20, 30, 2.59))

    def test_joins_relief_just_above_base_without_lowering_its_top(self):
        raw_stl(self.rest, Manifold.cube((20, 30, 0.6)))
        raw_stl(self.ways, Manifold.cube((2, 4, 1.5)).translate((4, 5, 0.6005)))

        finalize_stls(self.directory)

        full, = _load_shells(self.full)
        rest, = _load_shells(self.rest)
        paths, = _load_shells(self.ways)
        self.assertAlmostEqual(full.bounding_box()[5], 2.1005, places=6)
        self.assertAlmostEqual(paths.bounding_box()[5], 2.1005, places=6)
        self.assertAlmostEqual(rest.volume() + paths.volume(), full.volume(), places=4)
        self.assertAlmostEqual((rest ^ paths).volume(), 0)

    def test_drops_numerical_void_but_not_floating_relief_of_same_size(self):
        base = Manifold.cube((20, 30, 0.6))
        chip = Manifold.cube((0.2, 0.2, 0.06))
        repaired, feet = _join_relief((base - chip.translate((2, 3, 0.2))).decompose())
        self.assertAlmostEqual(repaired.volume(), base.volume())
        self.assertEqual(feet, [])
        with self.assertRaisesRegex(ValueError, "not attached to the base"):
            _join_relief((base + chip.translate((2, 3, 2))).decompose())

    def test_single_and_split_exports_keep_the_same_sloped_relief(self):
        base = Manifold.cube((20, 30, 0.6))
        hill = Manifold.cylinder(2, 4, 2).translate((5, 5, 0.6005))
        raw_stl(self.rest, base)
        raw_stl(self.ways, hill)

        finalize_stls(self.directory)

        full, = _load_shells(self.full)
        rest, = _load_shells(self.rest)
        ways, = _load_shells(self.ways)
        combined = rest + ways
        self.assertLess((full - combined).volume() + (combined - full).volume(), 1e-4)

    def test_joins_near_base_relief_larger_than_plate(self):
        base = Manifold.cube((20, 30, 0.6))
        relief = Manifold.cube((18, 28, 2)).translate((1, 1, 0.6005))
        self.assertGreater(relief.volume(), base.volume())
        raw_stl(self.rest, base, relief)
        raw_stl(self.ways)

        finalize_stls(self.directory)

        full, = _load_shells(self.full)
        self.assertAlmostEqual(full.bounding_box()[2], 0)
        self.assertAlmostEqual(full.bounding_box()[5], 2.6005, places=6)

    def test_collapses_redundant_edge_vertex_without_moving_surface(self):
        cube = Manifold.cube((10, 10, 1)).translate((180, 0, 0))
        mesh = cube.to_mesh64()
        vertices = mesh.vert_properties
        a, b = [i for i, v in enumerate(vertices) if v[1] == v[2] == 0]
        extra = vertices[a] + (vertices[b] - vertices[a]) * 1e-7
        np.testing.assert_array_equal(extra.astype(np.float32), vertices[a].astype(np.float32))
        split = len(vertices)
        faces = []
        for face in mesh.tri_verts.tolist():
            if a in face and b in face:
                k = next(i for i in range(3) if {face[i], face[(i + 1) % 3]} == {a, b})
                start, end, opposite = face[k:] + face[:k]
                faces.extend([(start, split, opposite), (split, end, opposite)])
            else:
                faces.append(face)
        indexed = Mesh64(np.vstack([vertices, extra]), np.array(faces, dtype=np.uint64))

        facets = _export_facets("map.stl", Manifold(indexed), single_piece=True)

        restored = _mesh(facets["vertices"], "map.stl")
        self.assertEqual(restored.bounding_box()[2], 0)
        self.assertEqual(len(restored.decompose()), 1)
        self.assertAlmostEqual(restored.volume(), cube.volume())

    def test_rejects_crossing_closed_surfaces_with_matching_volume(self):
        cubes = (
            Manifold.cube((2, 2, 2)),
            Manifold.cube((2, 2, 2)).translate((1, 1, 1)),
        )
        points = np.concatenate([mesh.vert_properties[mesh.tri_verts] for mesh in (
            cube.to_mesh64() for cube in cubes
        )])
        volume = sum(cube.volume() for cube in cubes)
        self.assertAlmostEqual(_mesh(points, "map-ways.stl").volume(), volume)
        with self.assertRaisesRegex(ValueError, "crossing faces"):
            _stl_facets("map-ways.stl", points, volume, single_piece=False)

    def test_rejects_opposing_walls_that_touch_after_rounding(self):
        left = Manifold.cube((1, 2, 2)).translate((180, 0, 0))
        right = Manifold.cube((1, 2, 2)).translate((181.000001, 1, 1))
        self.assertGreater(right.bounding_box()[0], left.bounding_box()[3])
        self.assertEqual(np.float32(right.bounding_box()[0]), np.float32(left.bounding_box()[3]))

        with self.assertRaisesRegex(ValueError, "crossing faces"):
            _export_facets("map-ways.stl", Manifold.compose([left, right]), single_piece=False)

    def test_vector_repair_keeps_narrow_ways_and_separates_point_contacts(self):
        plate = Manifold.cube((10, 10, 0.6))
        building = Manifold.cube((2, 2, 2.5))
        raw_stl(self.rest, plate, building.translate((2, 2, 0.59)), building.translate((4, 4, 0.59)))
        raw_stl(self.ways, Manifold.cube((0.07, 6, 1.0)).translate((3, 1, 0.59)))
        with patch("touch_mapper.print_mesh._solid_facets", side_effect=ValueError("unsafe STL")):
            finalize_stls(self.directory, blender_relief=True)

        full, = _load_shells(self.full)
        rest, = _load_shells(self.rest)
        ways = _load_shells(self.ways)
        self.assertEqual(full.bounding_box()[2], 0)
        self.assertAlmostEqual(full.bounding_box()[5], 3.09, delta=0.0001)
        self.assertAlmostEqual(rest.volume(), 60 + 8 * (3.09 - 0.6), delta=0.002)
        self.assertAlmostEqual(sum(part.volume() for part in ways), 0.07 * 4 * 0.99, delta=0.002)
        combined = Manifold.batch_boolean([rest, *ways], OpType.Add)
        self.assertAlmostEqual((combined - full).volume() + (full - combined).volume(), 0, delta=1e-4)

    def test_vector_repair_preserves_marker_with_touching_and_overhanging_buildings(self):
        plate = Manifold.cube((10, 10, 0.6))
        cap = Manifold.cube((3.6, 3.6, 4.49 - 2.768)).translate((6.4, 6.4, 2.768))
        support = Manifold.cube((3.24, 3.24, 2.2)).translate((6.76, 6.76, 0.59))
        touching = Manifold.cube((1, 1, 2.9)).translate((5.4, 5.4, 0.59))
        overhanging = Manifold.cube((0.2, 1, 2.9)).translate((9.9, 4, 0.59))
        tall_way = Manifold.cube((0.3, 1.2, 2.9)).translate((7.5, 6.1, 0.59))
        raw_stl(self.rest, plate, cap, support, touching, overhanging)
        raw_stl(self.ways, tall_way)
        first = True

        def fail_once(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                raise ValueError("unsafe STL")
            return _export_facets(*args, **kwargs)

        with patch("touch_mapper.print_mesh._export_facets", side_effect=fail_once):
            finalize_stls(self.directory, blender_relief=True, has_borders=True)
        gap = Manifold.cube((0.1, 0.1, 0.1)).translate((6.5, 6.5, 1.5))
        separated_edge = Manifold.cube((0.001, 0.001, 0.01)).translate((6.401, 6.401, 3.2))
        for path in (self.full, self.rest):
            solid, = _load_shells(path)
            self.assertTrue((solid ^ gap).is_empty())
            self.assertTrue((solid ^ separated_edge).is_empty())
            self.assertAlmostEqual((solid ^ gap.translate((0, 0, 1.8))).volume(), gap.volume())
            self.assertAlmostEqual(solid.bounding_box()[3], 10.1, delta=0.0003)
            self.assertGreater((solid ^ touching).volume(), 2.89)
        full, = _load_shells(self.full)
        rest, = _load_shells(self.rest)
        ways = Manifold.batch_boolean(_load_shells(self.ways), OpType.Add)
        self.assertGreater(ways.volume(), 0)
        self.assertAlmostEqual((rest ^ ways).volume(), 0)
        self.assertAlmostEqual((rest + ways - full).volume() + (full - (rest + ways)).volume(), 0, delta=1e-5)
