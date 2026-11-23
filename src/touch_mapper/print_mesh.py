import math
import struct
from pathlib import Path

import numpy as np
from manifold3d import Error, Manifold, Mesh64, OpType

HEADER = struct.Struct("<80sI")
FACET = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")])
# Allow for the upstream converter's floating-point origin.
TOLERANCE_MM = 0.001


def _error(name, reason):
    return ValueError(f"cannot make {name} printable: {reason}")


def _mesh(triangles, name):
    vertices = np.ascontiguousarray(triangles.reshape(-1, 3), dtype=np.float64)
    if not np.isfinite(vertices).all():
        raise _error(name, "STL has non-finite coordinates")
    faces = np.arange(len(vertices), dtype=np.uint64).reshape(-1, 3)
    mesh = Mesh64(vertices, faces)
    mesh.merge()  # Stitch the unindexed STL facets into oriented shells.
    solid = Manifold(mesh)
    if solid.status() != Error.NoError:
        raise _error(name, "STL has an open or invalid surface")
    return solid


def _load_shells(path):
    with path.open("rb") as stl:
        header = stl.read(HEADER.size)
        if len(header) != HEADER.size:
            raise _error(path.name, "invalid binary STL header")
        _, count = HEADER.unpack(header)
        expected_size = HEADER.size + FACET.itemsize * count
        if path.stat().st_size != expected_size:
            raise _error(path.name, "invalid binary STL length")
        if count == 0:
            return []
        solid = _mesh(np.fromfile(stl, dtype=FACET, count=count)["vertices"], path.name)
    if solid.is_empty():
        raise _error(path.name, "STL has no solid geometry")

    shells = []
    for shell in solid.decompose():
        if shell.volume() < 0:
            mesh = shell.to_mesh64()
            shell = Manifold(Mesh64(np.array(mesh.vert_properties), np.array(mesh.tri_verts[:, ::-1])))
        if shell.status() != Error.NoError or shell.volume() <= 0:
            raise _error(path.name, "STL has a shell with no enclosed volume")
        shells.append(shell)
    return shells


def _stl_facets(name, points, volume, single_piece):
    normals = np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    if not len(points) or (lengths == 0).any():
        raise _error(name, "solid did not survive STL precision")
    facets = np.zeros(len(points), dtype=FACET)
    facets["vertices"] = points
    facets["normal"] = normals / lengths[:, None]
    quantized = _mesh(facets["vertices"], name)
    if quantized.is_empty() or not math.isclose(quantized.volume(), volume, rel_tol=0.001):
        raise _error(name, "solid did not survive STL export")
    if single_piece and len(quantized.decompose()) != 1:
        raise _error(name, "relief is not attached to the base")
    return facets


def _write_stl(path, solid, single_piece=False):
    if solid.status() != Error.NoError or (single_piece and solid.is_empty()):
        raise _error(path.name, "solid assembly failed")
    facets = np.zeros(0, dtype=FACET)
    if not solid.is_empty():
        mesh = solid.to_mesh64()
        vertices = mesh.vert_properties.astype(np.float32).astype(np.float64)
        volume = solid.volume()
        try:
            facets = _stl_facets(path.name, vertices[mesh.tri_verts], volume, single_piece)
        except ValueError:
            # Keep indices during repair so float32-collapsed edges can be stitched.
            rounded = Manifold(Mesh64(vertices, np.array(mesh.tri_verts)))
            if rounded.status() != Error.NoError:
                raise
            mesh = rounded.to_mesh64()
            vertices = mesh.vert_properties.astype(np.float32).astype(np.float64)
            facets = _stl_facets(path.name, vertices[mesh.tri_verts], volume, single_piece)

    with path.open("wb") as stl:
        stl.write(HEADER.pack(b"Touch Mapper printable STL (mm)", len(facets)))
        facets.tofile(stl)


def _trim_below_base(solid):
    if not solid.is_empty() and solid.bounding_box()[2] < 0:
        return solid.trim_by_plane((0, 0, 1), 0)
    return solid


def finalize_stls(directory: Path):
    rest_path, ways_path = (directory / name for name in ("map-rest.stl", "map-ways.stl"))
    rest = Manifold.batch_boolean(_load_shells(rest_path), OpType.Add)
    ways = Manifold.batch_boolean(_load_shells(ways_path), OpType.Add)
    if rest.status() != Error.NoError or rest.is_empty():
        raise _error(rest_path.name, "the map is missing a solid base")
    if ways.status() != Error.NoError:
        raise _error(ways_path.name, "the ways could not be joined")
    # Blender puts the base at Z=0, but relief expansion can reach below it.
    if rest.bounding_box()[2] < -TOLERANCE_MM or (
        not ways.is_empty() and ways.bounding_box()[2] < -TOLERANCE_MM
    ):
        rest = _trim_below_base(rest)
        ways = _trim_below_base(ways)
    full = rest + ways
    if full.status() != Error.NoError or full.is_empty():
        raise _error("map.stl", "solid assembly failed")
    bottom = full.bounding_box()[2]
    if abs(bottom) > TOLERANCE_MM:
        raise _error("map.stl", "the base is not on the build plane")
    offset = (0, 0, -bottom)
    # The split models share coordinates; the base and other relief keep priority.
    _write_stl(directory / "map.stl", full.translate(offset), single_piece=True)
    _write_stl(rest_path, rest.translate(offset))
    _write_stl(ways_path, (ways - rest).translate(offset))
