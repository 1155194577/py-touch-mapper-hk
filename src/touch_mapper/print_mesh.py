import math
import struct
from pathlib import Path

import numpy as np
from manifold3d import Error, Manifold, Mesh64, OpType
from pymeshfix import PyTMesh

HEADER = struct.Struct("<80sI")
FACET = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")])
# Allow for the upstream converter's floating-point origin.
TOLERANCE_MM = 0.001
MAX_SEAM_SPAN_MM = 8
# Match the pinned engine's base height and overlap in tactile_constants.py.
BASE_HEIGHT_MM = 0.6
BASE_JOIN_MM = 0.01
MAX_MICRO_VOID_MM3 = 0.005


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


def _repair_surface(path):
    mesh = PyTMesh()
    mesh.set_quiet(True)
    mesh.load_file(str(path))
    vertices, faces = mesh.return_arrays()
    edges = np.sort(np.stack((faces, faces[:, [1, 2, 0]]), axis=-1).reshape(-1, 2), axis=1)
    edges, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = {}
    for start, end in edges[counts == 1]:
        boundary.setdefault(start, set()).add(end)
        boundary.setdefault(end, set()).add(start)
    while boundary:
        loop = set()
        pending = [next(iter(boundary))]
        while pending:
            vertex = pending.pop()
            if vertex not in loop:
                loop.add(vertex)
                pending.extend(boundary.pop(vertex))
        if np.linalg.norm(np.ptp(vertices[list(loop)], axis=0)) > MAX_SEAM_SPAN_MM:
            raise _error(path.name, "STL has an open or invalid surface")
    # Cap local seams only; do not clean/remesh the rest of the surface.
    mesh.fill_small_boundaries(refine=False)
    if mesh.n_boundaries:
        raise _error(path.name, "STL has an open or invalid surface")
    vertices, faces = mesh.return_arrays()
    return Manifold(Mesh64(vertices, faces.astype(np.uint64)))


def _load_shells(path, repair=False):
    repaired = False
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
        triangles = np.fromfile(stl, dtype=FACET, count=count)["vertices"]
    try:
        solid = _mesh(triangles, path.name)
    except ValueError:
        if not repair or not np.isfinite(triangles).all():
            raise
        solid = _repair_surface(path)
        if solid.status() != Error.NoError:
            raise
        repaired = True
    if solid.is_empty():
        raise _error(path.name, "STL has no solid geometry")

    shells = []
    for shell in solid.decompose():
        if shell.volume() < 0:
            mesh = shell.to_mesh64()
            shell = Manifold(Mesh64(np.array(mesh.vert_properties), np.array(mesh.tri_verts[:, ::-1])))
        if repaired and shell.volume() == 0:
            continue
        if shell.status() != Error.NoError or shell.volume() <= 0:
            raise _error(path.name, "STL has a shell with no enclosed volume")
        shells.append(shell)
    if not shells:
        raise _error(path.name, "STL has no solid geometry")
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
    parts = quantized.decompose()
    if single_piece and (len(parts) != 1 or quantized.bounding_box()[2] != 0):
        raise _error(name, "relief is not attached to the base")
    if any(p.volume() == 0 for p in parts):
        raise _error(name, "STL export contains a shell with no enclosed volume")
    return facets


def _separate_facets(name, mesh, volume, single_piece):
    vertices = mesh.vert_properties.astype(np.float32)
    faces = mesh.tri_verts.astype(np.intp)
    points = vertices[faces].astype(np.float64)
    normals = np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0])
    _, inverse, counts = np.unique(vertices, axis=0, return_inverse=True, return_counts=True)
    affected = np.union1d(faces[(normals == 0).all(axis=1)], np.flatnonzero(counts[inverse] > 1))
    # Resolve float32 coincidences with repeatable, submicron offsets.
    for step in (0.00005, 0.0001, 0.0002, 0.0005):
        trial = vertices.copy()
        delta = np.random.default_rng(0).uniform(-step, step, (len(affected), 3))
        delta[vertices[affected, 2] == 0, 2] = 0
        trial[affected] += delta
        if single_piece:
            trial[:, 2] = np.maximum(trial[:, 2], 0)
        try:
            return _stl_facets(name, trial[faces].astype(np.float64), volume, single_piece)
        except ValueError:
            pass
    raise _error(name, "solid did not survive STL precision")


def _export_facets(name, solid, single_piece):
    mesh = solid.to_mesh64()
    vertices = mesh.vert_properties.astype(np.float32).astype(np.float64)
    volume = solid.volume()
    try:
        return _stl_facets(name, vertices[mesh.tri_verts], volume, single_piece)
    except ValueError:
        pass
    # Keep indices during repair so float32-collapsed edges can be stitched.
    rounded = Manifold(Mesh64(vertices, np.array(mesh.tri_verts)))
    if rounded.status() == Error.NoError and not rounded.is_empty():
        indexed = rounded.to_mesh64()
        vertices = indexed.vert_properties.astype(np.float32).astype(np.float64)
        try:
            return _stl_facets(name, vertices[indexed.tri_verts], volume, single_piece)
        except ValueError:
            pass
    return _separate_facets(name, mesh, volume, single_piece)


def _write_stl(path, solid, single_piece=False):
    if solid.status() != Error.NoError or (single_piece and solid.is_empty()):
        raise _error(path.name, "solid assembly failed")
    facets = (np.zeros(0, dtype=FACET) if solid.is_empty()
              else _export_facets(path.name, solid, single_piece))
    with path.open("wb") as stl:
        stl.write(HEADER.pack(b"Touch Mapper printable STL (mm)", len(facets)))
        facets.tofile(stl)


def _trim_below_base(solid):
    if not solid.is_empty() and solid.bounding_box()[2] < 0:
        return solid.trim_by_plane((0, 0, 1), 0)
    return solid


def _is_sliver(component):
    x0, y0, z0, x1, y1, z1 = component.bounding_box()
    volume = component.volume()
    # Near-zero negative shells bound voids, not detached tactile material.
    return -MAX_MICRO_VOID_MM3 <= volume <= 1e-6 or (
        abs(volume) < 0.06 and min(x1 - x0, y1 - y0, z1 - z0) < 0.03
    )


def _without_slivers(solid):
    parts = solid.decompose()
    kept = [p for p in parts if not _is_sliver(p)]
    return Manifold.compose(kept) if len(kept) < len(parts) else solid


def _join_relief(parts):
    body = max(
        (part for part in parts if abs(part.bounding_box()[2]) <= TOLERANCE_MM),
        key=lambda part: part.volume(),
        default=None,
    )
    if body is None:
        raise _error("map.stl", "the base is not on the build plane")
    kept = [body]
    feet = []
    bx0, by0, ground, bx1, by1, _ = body.bounding_box()
    base = ground + BASE_HEIGHT_MM
    for part in parts:
        if part is body or _is_sliver(part):
            continue
        x0, y0, bottom, x1, y1, top = part.bounding_box()
        if (part.volume() <= 0 or abs(bottom - base) > TOLERANCE_MM or top - bottom < BASE_JOIN_MM
                or not (bx0 <= x0 <= x1 <= bx1 and by0 <= y0 <= y1 <= by1)):
            raise _error("map.stl", "relief is not attached to the base")
        # Attach only the foot; both exports keep the original relief above it.
        stretch = (top - base + BASE_JOIN_MM) / (top - bottom)
        anchor = part.scale((1, 1, stretch)).translate((0, 0, top * (1 - stretch)))
        kept.append(part)
        feet.append(anchor.trim_by_plane((0, 0, -1), -(bottom + BASE_JOIN_MM)))
    joined = Manifold.batch_boolean([*kept, *feet], OpType.Add)
    body, *detached = sorted(joined.decompose(), key=lambda part: part.volume(), reverse=True)
    if any(not _is_sliver(part) for part in detached):
        raise _error("map.stl", "relief is not attached to the base")
    return body, feet


def finalize_stls(directory: Path):
    rest_path, ways_path = (directory / name for name in ("map-rest.stl", "map-ways.stl"))
    rest = Manifold.batch_boolean(_load_shells(rest_path, repair=True), OpType.Add)
    ways = Manifold.batch_boolean(_load_shells(ways_path, repair=True), OpType.Add)
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
    parts = full.decompose()
    if len(parts) > 1:
        full, feet = _join_relief(parts)
        if feet:
            rest = Manifold.batch_boolean([rest, *feet], OpType.Add)
        rest = _without_slivers(rest)
    bottom = full.bounding_box()[2]
    if abs(bottom) > TOLERANCE_MM:
        raise _error("map.stl", "the base is not on the build plane")
    offset = (0, 0, -bottom)
    # The split models share coordinates; the base and other relief keep priority.
    _write_stl(directory / "map.stl", full.translate(offset), single_piece=True)
    _write_stl(rest_path, rest.translate(offset))
    _write_stl(ways_path, _without_slivers(ways - rest).translate(offset))
