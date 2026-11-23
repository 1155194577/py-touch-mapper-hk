import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
from manifold3d import Manifold, Mesh64

from touch_mapper.print_mesh import _join_relief, _load_shells, _mesh, _separate_facets, finalize_stls


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

    def test_separates_vertices_that_coincide_in_float32(self):
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

        facets = _separate_facets("map.stl", indexed, cube.volume(), single_piece=True)

        restored = _mesh(facets["vertices"], "map.stl")
        self.assertEqual(restored.bounding_box()[2], 0)
        self.assertEqual(len(restored.decompose()), 1)
        self.assertAlmostEqual(restored.volume(), cube.volume(), places=2)
