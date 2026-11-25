import math
import struct
from pathlib import Path

import numpy as np
from manifold3d import Error, Manifold, Mesh64, OpType
from pymeshfix import PyTMesh
from shapely.errors import GEOSException

from .relief_mesh import SEAM_STEP, XY_STEP, Z_STEP, reconstruct_relief

HEADER = struct.Struct("<80sI")
FACET = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")])
# Allow for the upstream converter's floating-point origin.
TOLERANCE_MM = 0.001
MAX_SEAM_SPAN_MM = 8
# A larger hole is capped only when it is a vertical strip standing on the base: missing building walls.
PLANAR_SEAM_MM = 0.001
# Match the pinned engine's base height and overlap in tactile_constants.py.
BASE_HEIGHT_MM = 0.6
BASE_JOIN_MM = 0.01
BUILDING_HEIGHT_MM = 2.9
BLENDER_FATTEN_UNITS = 0.05
CORNER_WIDTH_MM = 3 * 1.2
CORNER_BOTTOM_MM = 2.2 * 0.99
CORNER_TOP_MM = 3.9
MAX_MICRO_VOID_MM3 = 0.005
MAX_DEBRIS_MM3 = 0.06


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


def _is_wall(points, ground):
    # Each corner of a missing strip of building wall appears at its foot and its top.
    _, counts = np.unique(np.rint(points[:, :2] / PLANAR_SEAM_MM), axis=0, return_counts=True)
    raised = points[:, 2].min() >= ground + BASE_HEIGHT_MM - 2 * BASE_JOIN_MM
    return raised and (counts == 2).all()


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
        points = vertices[list(loop)]
        if np.linalg.norm(np.ptp(points, axis=0)) > MAX_SEAM_SPAN_MM and not _is_wall(points, vertices[:, 2].min()):
            raise _error(path.name, "STL has an open or invalid surface")
    # Cap local seams only; do not clean/remesh the rest of the surface.
    mesh.fill_small_boundaries(refine=False)
    vertices, faces = mesh.return_arrays()
    if mesh.n_boundaries:
        faces = _cap_triangular_holes(faces)
        if faces is None:
            raise _error(path.name, "STL has an open or invalid surface")
    return Manifold(Mesh64(vertices, faces.astype(np.uint64)))


def _cap_triangular_holes(faces):
    # The filler can leave three-edge holes open; close each with one facet.
    edges = np.stack((faces, faces[:, [1, 2, 0]]), axis=-1).reshape(-1, 2)
    inner = {tuple(edge) for edge in edges}
    following = {}
    for start, end in edges:
        if (end, start) not in inner:
            if start in following:
                return None
            following[start] = end
    caps = []
    while following:
        a, b = following.popitem()
        c = following.pop(b, None)
        if c is None or following.pop(c, None) != a:
            return None
        caps.append((a, c, b))
    return np.r_[faces, np.array(caps, dtype=faces.dtype)]


def _read_triangles(path):
    with path.open("rb") as stl:
        header = stl.read(HEADER.size)
        if len(header) != HEADER.size:
            raise _error(path.name, "invalid binary STL header")
        _, count = HEADER.unpack(header)
        expected_size = HEADER.size + FACET.itemsize * count
        if path.stat().st_size != expected_size:
            raise _error(path.name, "invalid binary STL length")
        return np.fromfile(stl, dtype=FACET, count=count)["vertices"]


def _load_shells(path, repair=False):
    repaired = False
    triangles = _read_triangles(path)
    if not len(triangles):
        return []
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
    points = points.astype(np.float32).astype(np.float64)
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
    # Manifold validates connectivity, but does not rule out crossing faces.
    vertices, indices = np.unique(points.reshape(-1, 3), axis=0, return_inverse=True)
    surface = PyTMesh()
    surface.set_quiet(True)
    surface.load_array(vertices, indices.reshape(-1, 3).astype(np.int32))
    if surface.n_boundaries or surface.n_faces != len(facets):
        raise _error(name, "solid did not survive STL precision")
    if len(surface.select_intersecting_triangles()):
        raise _error(name, "STL export has crossing faces")
    return facets


def _export_facets(name, solid, single_piece):
    mesh = solid.to_mesh64()
    volume = solid.volume()
    try:
        return _stl_facets(name, mesh.vert_properties[mesh.tri_verts], volume, single_piece)
    except ValueError as error:
        # Keep indices during repair so float32-collapsed edges can be stitched.
        vertices = mesh.vert_properties.astype(np.float32).astype(np.float64)
        rounded = Manifold(Mesh64(vertices, np.array(mesh.tri_verts)))
        if rounded.status() != Error.NoError or rounded.is_empty():
            raise error
        indexed = rounded.to_mesh64()
        return _stl_facets(name, indexed.vert_properties[indexed.tri_verts], volume, single_piece)


def _solid_facets(name, solid, single_piece=False):
    if solid.status() != Error.NoError or (single_piece and solid.is_empty()):
        raise _error(name, "solid assembly failed")
    return np.zeros(0, dtype=FACET) if solid.is_empty() else _export_facets(name, solid, single_piece)


def _write_stl(path, facets):
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


def _base_body(parts):
    return max(
        (part for part in parts if abs(part.bounding_box()[2]) <= TOLERANCE_MM),
        key=lambda part: part.volume(),
        default=None,
    )


def _separate_blender_roofs(parts, ways, scale, building_height):
    body = _base_body(parts)
    if body is None or building_height == 0:
        return parts, False
    ground = body.bounding_box()[2]
    roof = ground + BASE_HEIGHT_MM - BASE_JOIN_MM + building_height
    margin = 1000 * BLENDER_FATTEN_UNITS / scale + TOLERANCE_MM
    footprint = body.slice(ground + BASE_JOIN_MM)
    kept = []
    for part in parts:
        _, _, low, _, _, high = part.bounding_box()
        # Blender can export a closed roof skin without its walls after fattening.
        if (low > ground + BASE_HEIGHT_MM + TOLERANCE_MM
                and roof - margin <= low <= roof <= high <= roof + margin
                and part.volume() > 0 and (part ^ ways).is_empty()
                and not part.project().is_empty() and (part.project() - footprint).is_empty()):
            continue
        kept.append(part)
    return kept, len(kept) != len(parts)


def _split_way_debris(parts, ways):
    # Blender can leave sub-printable way fragments floating above the base.
    body = _base_body(parts)
    if body is None or ways.is_empty():
        return parts, []
    base = body.bounding_box()[2] + BASE_HEIGHT_MM
    kept, debris = [], []
    for part in parts:
        floating = (part is not body and 0 < part.volume() < MAX_DEBRIS_MM3
                    and part.bounding_box()[2] > base + TOLERANCE_MM
                    and (part - ways).volume() < 0.01 * part.volume())
        (debris if floating else kept).append(part)
    return kept, debris


def _join_relief(parts):
    body = _base_body(parts)
    if body is None:
        raise _error("map.stl", "the base is not on the build plane")
    kept = [body]
    feet = []
    bx0, by0, ground, bx1, by1, _ = body.bounding_box()
    base = ground + BASE_HEIGHT_MM
    for part in parts:
        # A negative shell bounds an enclosed void; filling it leaves the outer surface unchanged.
        if part is body or _is_sliver(part) or part.volume() < 0:
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


def _split_corner_top(rest, bottom):
    # The pinned Blender engine exports the northeast marker cap as its own
    # cuboid, three border widths wide. It is not supported at every XY point.
    floor = rest[(abs(rest[:, :, 2]) < TOLERANCE_MM).all(axis=1)]
    if not len(floor):
        raise ValueError("cannot locate the map floor")
    normals = np.cross(floor[:, 1] - floor[:, 0], floor[:, 2] - floor[:, 0])
    floor = floor[normals[:, 2] < 0]
    if not len(floor):
        raise ValueError("cannot locate the map floor")
    largest = np.argsort(normals[normals[:, 2] < 0, 2])[:2]
    xy = floor[largest, :, :2].max(axis=(0, 1))
    offset = BASE_HEIGHT_MM - BASE_JOIN_MM
    low = np.r_[xy - CORNER_WIDTH_MM, offset + CORNER_BOTTOM_MM]
    high = np.r_[xy, offset + CORNER_TOP_MM]
    extreme = (abs(rest - low) < TOLERANCE_MM) | (abs(rest - high) < TOLERANCE_MM)
    cap = extreme.all(axis=(1, 2))
    if np.count_nonzero(cap) != 12:
        raise ValueError("cannot isolate the overhanging corner marker")
    points = rest[cap].astype(np.float64)
    points[:, :, 2] -= bottom
    steps = np.array([XY_STEP, XY_STEP, Z_STEP])
    points = np.rint(points / steps) * steps
    lo, hi = points.min(axis=(0, 1)), points.max(axis=(0, 1))
    # Enforce one coordinate per side after the Blender-to-mm conversion.
    points = np.where(abs(points - lo) < abs(points - hi), lo, hi)
    marker = _mesh(points, "corner marker")
    if marker.is_empty() or not math.isclose(marker.volume(), np.prod(hi - lo)):
        raise ValueError("invalid overhanging corner marker")
    return rest[~cap], marker


def _roof_solids(paths, vertices, surfaces):
    for slot, (path, surface) in enumerate(zip(paths, surfaces)):
        if not len(surface) and slot == 2:
            yield Manifold()
            continue
        solid = Manifold(Mesh64(vertices, surface.astype(np.uint64)))
        if solid.status() != Error.NoError:
            raise _error(path.name, "invalid reconstructed relief")
        yield solid


def _restore_marker(paths, solids, marker):
    full, rest, ways = solids
    return (
        _solid_facets(paths[0].name, full + marker, single_piece=True),
        _solid_facets(paths[1].name, rest + marker),
        _solid_facets(paths[2].name, ways - marker)
        if not ways.is_empty() and ways.bounding_box()[5] > marker.bounding_box()[2]
        else None,
    )


def _reconstructed_facets(paths, bottom, has_borders, additional_relief, *, clip_ways=False, removed=()):
    try:
        rest, ways = (_read_triangles(path) for path in paths[1:])
        for x0, y0, z0, x1, y1, z1 in removed:
            lo, hi = np.array((x0, y0, z0)) - TOLERANCE_MM, np.array((x1, y1, z1)) + TOLERANCE_MM
            ways = ways[~((ways >= lo) & (ways <= hi)).all(axis=(1, 2))]
        marker = None
        if has_borders:
            rest, marker = _split_corner_top(rest, bottom)
        if additional_relief is not None and not additional_relief.is_empty():
            dots = additional_relief.to_mesh64()
            rest = np.concatenate((rest, dots.vert_properties[dots.tri_verts]))
        vertices, surfaces = reconstruct_relief(rest, ways, bottom, clip_ways=clip_ways)
    except (ValueError, GEOSException) as exc:
        raise _error(paths[0].name, str(exc)) from exc
    solids = tuple(_roof_solids(paths, vertices, surfaces))
    restored = (None, None, None)
    if marker is not None:
        try:
            restored = _restore_marker(paths, solids, marker)
        except ValueError:
            # A relief corner can touch the cap along a single vertical edge.
            # Inset the interior sides, without moving the map's outside edges.
            lo, hi = np.array(marker.bounding_box()).reshape(2, 3)
            lo[:2] += SEAM_STEP
            inset = Manifold.cube(hi - lo).translate(lo)
            restored = _restore_marker(paths, solids, inset)
    for slot, (path, surface, solid, facets) in enumerate(zip(paths, surfaces, solids, restored)):
        if facets is not None:
            yield facets
        elif solid.is_empty() and slot == 2:
            yield np.zeros(0, dtype=FACET)
        else:
            yield _stl_facets(path.name, vertices[surface], solid.volume(), slot == 0)


def _additional_relief(build, rest, ways):
    relief, count = build(rest, ways)
    if relief.status() != Error.NoError:
        raise _error("map.stl", "invalid additional relief")
    return relief, count


def finalize_stls(directory: Path, *, blender_relief=False, has_borders=False, build_additional_relief=None,
                  blender_scale=None, building_height_mm=None):
    rest_path, ways_path = (directory / name for name in ("map-rest.stl", "map-ways.stl"))
    rest = Manifold.batch_boolean(_load_shells(rest_path, repair=True), OpType.Add)
    ways = Manifold.batch_boolean(_load_shells(ways_path, repair=True), OpType.Add)
    if rest.status() != Error.NoError or rest.is_empty():
        raise _error(rest_path.name, "the map is missing a solid base")
    if ways.status() != Error.NoError:
        raise _error(ways_path.name, "the ways could not be joined")
    additional_relief = None
    exposed_count = 0
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
    reconstruct_roofs = False
    if len(parts) > 1 and blender_relief and blender_scale is not None:
        height = BUILDING_HEIGHT_MM if building_height_mm is None else building_height_mm
        parts, reconstruct_roofs = _separate_blender_roofs(parts, ways, blender_scale, height)
    if not reconstruct_roofs and build_additional_relief is not None:
        additional_relief, exposed_count = _additional_relief(build_additional_relief, rest, ways)
        if exposed_count:
            rest += additional_relief
            if rest.bounding_box()[2] < -TOLERANCE_MM:
                rest = _trim_below_base(rest)
            full = rest + ways
            if full.status() != Error.NoError or full.is_empty():
                raise _error("map.stl", "solid assembly failed")
            parts = full.decompose()
    removed = []
    if len(parts) > 1 or reconstruct_roofs:
        parts, debris = _split_way_debris(parts, ways)
        removed = [part.bounding_box() for part in debris]
        if debris:
            debris = Manifold.batch_boolean(debris, OpType.Add)
            rest, ways = rest - debris, ways - debris
        full, feet = _join_relief(parts)
        if feet:
            rest = Manifold.batch_boolean([rest, *feet], OpType.Add)
        rest = _without_slivers(rest)
    bottom = full.bounding_box()[2]
    if abs(bottom) > TOLERANCE_MM:
        raise _error("map.stl", "the base is not on the build plane")
    offset = (0, 0, -bottom)
    # The split models share coordinates; the base and other relief keep priority.
    paths = (directory / "map.stl", rest_path, ways_path)
    facets = None
    if reconstruct_roofs:
        facets = tuple(_reconstructed_facets(paths, bottom, has_borders, None, clip_ways=not has_borders, removed=removed))
        if build_additional_relief is not None:
            rebuilt_rest, rebuilt_ways = (
                _mesh(facets[slot]["vertices"], paths[slot].name) if len(facets[slot]) else Manifold()
                for slot in (1, 2)
            )
            additional_relief, exposed_count = _additional_relief(build_additional_relief, rebuilt_rest, rebuilt_ways)
            if exposed_count:
                raised = additional_relief.translate((0, 0, bottom))
                facets = tuple(_reconstructed_facets(paths, bottom, has_borders, raised, clip_ways=not has_borders, removed=removed))
    else:
        try:
            facets = (
                _solid_facets(paths[0].name, full.translate(offset), single_piece=True),
                _solid_facets(rest_path.name, rest.translate(offset)),
                _solid_facets(ways_path.name, _without_slivers(ways - rest).translate(offset)),
            )
        except ValueError:
            if not blender_relief:
                raise
    if facets is None:
        facets = tuple(_reconstructed_facets(paths, bottom, has_borders, additional_relief, removed=removed))
    for path, data in zip(paths, facets):
        _write_stl(path, data)
    return exposed_count
