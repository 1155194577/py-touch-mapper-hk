"""Reconstruct Blender's supported relief as a piecewise-planar solid."""

from itertools import combinations

import numpy as np
import shapely as sh

# Binary steps keep the mesh on an exactly representable float32 lattice.
XY_STEP = 2 ** -11
Z_STEP = 2 ** -15
SEAM_STEP = 8 * XY_STEP
# Bound the vertical adjustment when welding intersecting wall edges.
MAX_Z_CORRECTION = 0.025


def _planes(xy, z):
    vectors = np.concatenate((xy, z[:, :, None]), axis=2)
    delta = vectors[:, 1:] - vectors[:, :1]
    normal = np.cross(delta[:, 0], delta[:, 1])
    gradient = -normal[:, :2] / normal[:, 2:]
    return np.column_stack((gradient, z[:, 0] - (gradient * xy[:, 0]).sum(axis=1)))


def _evaluate(planes, xy):
    return np.einsum("ijk,ik->ij", xy, planes[:, :2]) + planes[:, 2, None]


def _crossing_lines(triangles, tree, planes, low, high):
    # Node not just the footprints but also where overlapping roof planes
    # trade height. This keeps each cell on a single source plane.
    for start in range(0, len(triangles), 512):
        a, b = tree.query(triangles[start:start + 512], predicate="intersects")
        a += start
        keep = (a < b) & (low[a] < high[b] - Z_STEP) & (low[b] < high[a] - Z_STEP)
        a, b = a[keep], b[keep]
        if not len(a):
            continue
        overlap, parent = sh.get_parts(
            sh.intersection(triangles[a], triangles[b], grid_size=XY_STEP), return_index=True,
        )
        polygon = sh.get_type_id(overlap) == 3
        a, b, overlap = a[parent[polygon]], b[parent[polygon]], overlap[polygon]
        if not len(a):
            continue
        delta = planes[a] - planes[b]
        xy, index = sh.get_coordinates(overlap, return_index=True)
        z = (xy * delta[index, :2]).sum(axis=1) + delta[index, 2]
        minimum, maximum = np.full(len(a), np.inf), np.full(len(a), -np.inf)
        np.minimum.at(minimum, index, z)
        np.maximum.at(maximum, index, z)
        crosses = (minimum < -Z_STEP) & (maximum > Z_STEP)
        delta, overlap = delta[crosses], overlap[crosses]
        if not len(overlap):
            continue
        center = sh.get_coordinates(sh.centroid(overlap))
        normal = delta[:, :2]
        power = (normal ** 2).sum(axis=1)
        center -= normal * (((center * normal).sum(axis=1) + delta[:, 2]) / power)[:, None]
        direction = np.column_stack((-normal[:, 1], normal[:, 0]))
        bounds = sh.bounds(overlap)
        span = 2 * np.linalg.norm(bounds[:, 2:] - bounds[:, :2], axis=1)
        offset = direction * (span / np.sqrt(power))[:, None]
        cut = sh.linestrings(np.stack((center - offset, center + offset), axis=1))
        segs = sh.get_parts(sh.intersection(overlap, cut, grid_size=XY_STEP))
        yield segs[sh.get_type_id(segs) == 1]


def _cells(lines):
    return sh.get_parts(sh.polygonize(sh.get_parts(sh.union_all(lines, grid_size=XY_STEP))))


def _triangulate(cells):
    triangles, cells = sh.get_parts(sh.constrained_delaunay_triangles(cells), return_index=True)
    return sh.get_coordinates(triangles).reshape(-1, 4, 2)[:, :3], cells


def _highest(size, cell, facet, heights):
    order = np.lexsort((-heights, cell))
    cell, facet = cell[order], facet[order]
    out = np.full(size, -1)
    if len(cell):
        first = np.r_[True, cell[:-1] != cell[1:]]
        out[cell[first]] = facet[first]
    return out


def _overlay(rest, ways, bottom, clip_ways):
    points = np.concatenate((rest, ways)).astype(np.float64)
    xy = points[:, :, :2]
    delta = xy[:, 1:] - xy[:, :1]
    upward = delta[:, 0, 0] * delta[:, 1, 1] > delta[:, 0, 1] * delta[:, 1, 0]
    is_rest = (np.arange(len(points)) < len(rest))[upward]
    xy = np.rint(xy[upward] / XY_STEP) * XY_STEP
    z = points[upward, :, 2] - bottom
    delta = xy[:, 1:] - xy[:, :1]
    keep = delta[:, 0, 0] * delta[:, 1, 1] > delta[:, 0, 1] * delta[:, 1, 0]
    xy, z, is_rest = xy[keep], z[keep], is_rest[keep]
    if not len(xy) or z.min() < -MAX_Z_CORRECTION:
        raise ValueError("invalid relief above the build plane")
    planes = _planes(xy, z)
    polygons = sh.polygons(xy)
    tree = sh.STRtree(polygons)
    cuts = _crossing_lines(polygons, tree, planes, z.min(axis=1), z.max(axis=1))
    cells = _cells(np.concatenate([sh.boundary(polygons), *cuts]))
    samples = sh.point_on_surface(cells)
    cell, facet = tree.query(samples, predicate="within")
    loc = sh.get_coordinates(samples)[cell]
    heights = (loc * planes[facet, :2]).sum(axis=1) + planes[facet, 2]
    full = _highest(len(cells), cell, facet, heights)
    base = is_rest[facet]
    rest_top = _highest(len(cells), cell[base], facet[base], heights[base])
    if clip_ways:
        keep = (rest_top >= 0) | (full < 0)
        cells, full, rest_top = cells[keep], full[keep], rest_top[keep]
    if not len(full) or (rest_top < 0).any():
        raise ValueError("relief is not fully supported by the base")
    xy, parent = _triangulate(cells)
    upper = _evaluate(planes[full[parent]], xy)
    lower = _evaluate(planes[rest_top[parent]], xy)
    if (lower - upper).max() > MAX_Z_CORRECTION:
        raise ValueError("relief could not be partitioned at intersecting roofs")
    return xy, np.stack((np.maximum(lower, upper), lower))


def _separate_pinches(xy, fields, pinches, fill=False):
    # At an X junction, two high sectors can touch along a vertical edge.
    # Break that contact in a sub-printable neighborhood, not across a facet.
    triangles = sh.polygons(xy)
    rects = sh.box(*np.c_[pinches - SEAM_STEP, pinches + SEAM_STEP].T)
    tree = sh.STRtree(triangles)
    patch, hit = tree.query(rects, predicate="intersects")
    keep = np.ones(len(xy), dtype=bool)
    keep[hit] = False
    # Node the kept neighbors too, so no cell spans the patch boundary.
    near = np.unique(tree.query(triangles[~keep], predicate="intersects")[1])
    cells = _cells(np.r_[sh.boundary(triangles[near]), sh.boundary(rects)])
    samples = sh.point_on_surface(cells)
    cell, source = tree.query(samples, predicate="within")
    replaced = np.ones(len(cells), dtype=bool)
    replaced[cell[keep[source]]] = False
    cell, first = np.unique(cell, return_index=True)
    source = source[first]
    cell, source = cell[replaced[cell]], source[replaced[cell]]
    # Snap rounding can fold a thin kept neighbor into a patch cell.
    border = near[keep[near]]
    a, b = sh.STRtree(cells[cell]).query(triangles[border], predicate="intersects")
    covered = np.zeros(len(border))
    np.add.at(covered, a, sh.area(sh.intersection(triangles[border[a]], cells[cell[b]])))
    keep[border[covered > sh.area(triangles[border]) / 2]] = False
    cell_in_patch, patch_id = sh.STRtree(rects).query(samples, predicate="within")
    region = np.full(len(cells), -1)
    region[cell_in_patch] = patch_id
    region = region[cell]
    local = region >= 0
    new_xy, parent = _triangulate(cells[cell])
    corrected = []
    for field in fields:
        planes = _planes(xy[source], field[source])
        # Carve first; a contact that walks along a tapering gap is filled.
        level = np.full(len(rects), -np.inf if fill else np.inf)
        (np.maximum if fill else np.minimum).at(level, patch, field[hit].max(axis=1) if fill else field[hit].min(axis=1))
        planes[local, :2] = 0
        planes[local, 2] = level[region[local]]
        corrected.append(np.r_[field[keep], _evaluate(planes[parent], new_xy)])
    return np.r_[xy[keep], new_xy], np.stack(corrected)


def _next(half_edge):
    return (half_edge // 3) * 3 + (half_edge + 1) % 3


def _weld_heights(nodes, heights, left, right):
    count = nodes.size
    node = np.tile(nodes.ravel(), len(heights))
    original = heights.ravel()
    order = np.lexsort((original, node))
    sorted_z = original[order]
    distinct = np.r_[True, (np.diff(node[order]) != 0) | (np.diff(sorted_z) > XY_STEP / 2)]
    anchors = np.rint(sorted_z[distinct] / Z_STEP) * Z_STEP
    z = np.empty_like(original)
    z[order] = anchors[np.cumsum(distinct) - 1]
    a, b = np.c_[left, _next(left)], np.c_[_next(right), right]
    intervals = (a, a + count, b, b + count)
    for _ in range(20):
        edits = []
        for a, b in combinations(intervals, 2):
            delta = z[a] - z[b]
            crossing = delta[:, 0] * delta[:, 1] < 0
            end = np.argmin(abs(delta[crossing]), axis=1)
            edits.extend(zip(a[crossing, end], b[crossing, end]))
        if not edits:
            if (abs(z - original) > MAX_Z_CORRECTION).any():
                break
            return z.reshape(heights.shape)
        # A snap-rounded intersection can fall just off an edge endpoint.
        # Coalesce levels at that endpoint before triangulating the wall.
        for a, b in edits:
            low, high = sorted((z[a], z[b]))
            z[(node == node[a]) & (z >= low) & (z <= high)] = low
    raise ValueError("roof seams need more than a local precision repair")


class _Roof:
    def __init__(self, xy, fields):
        delta = xy[:, 1:] - xy[:, :1]
        orient = delta[:, 0, 0] * delta[:, 1, 1] - delta[:, 0, 1] * delta[:, 1, 0]
        if (orient == 0).any():
            raise ValueError("relief contains a collapsed planar facet")
        xy, fields = xy.copy(), fields.copy()
        flip = orient < 0
        xy[flip], fields[:, flip] = xy[flip, ::-1], fields[:, flip, ::-1]
        coords, nodes = np.unique(xy.reshape(-1, 2), axis=0, return_inverse=True)
        nodes = nodes.reshape(-1)
        edges = np.sort(np.c_[nodes, nodes[_next(np.arange(len(nodes)))]], axis=1)
        _, groups, counts = np.unique(edges, axis=0, return_inverse=True, return_counts=True)
        if (counts > 2).any():
            raise ValueError("relief contains overlapping planar facets")
        ordered = np.argsort(groups.ravel(), kind="stable")
        starts = np.r_[0, np.cumsum(counts)[:-1]]
        self.left = ordered[starts]
        self.right = np.full(len(starts), -1)
        shared = counts == 2
        self.right[shared] = ordered[starts[shared] + 1]
        heights = _weld_heights(nodes, fields, self.left[shared], self.right[shared])
        self.heights = np.concatenate((heights, np.zeros_like(heights[:1])))
        if (self.heights[0] < self.heights[1]).any() or (self.heights[1] <= 0).any():
            raise ValueError("invalid relief layers")
        keys = np.c_[np.tile(nodes, len(self.heights)), self.heights.ravel()]
        levels, index = np.unique(keys, axis=0, return_inverse=True)
        self.vertices = np.c_[coords[levels[:, 0].astype(int)], levels[:, 1]]
        center = (coords.min(axis=0) + coords.max(axis=0)) / 2
        self.vertices = np.r_[self.vertices, [[*center, 0]]]
        self.index = index.reshape(self.heights.shape)

    @staticmethod
    def _difference(upper, lower, z_top, z_bottom, other_upper, other_lower,
                    other_top, other_bottom, neighbor):
        overlaps = neighbor & (other_top > other_bottom) & (other_top > z_bottom) & (other_bottom < z_top)
        below = overlaps & (other_bottom > z_bottom)
        above = overlaps & (other_top < z_top)
        whole = ~overlaps & (z_top > z_bottom)
        return np.r_[upper[whole], other_lower[below], upper[above]], np.r_[lower[whole], lower[below], other_upper[above]]

    def _walls(self, top, bottom):
        u, v = self.left, self.right
        shared = v >= 0
        neighbor = np.maximum(v, 0)
        upper, lower = np.c_[top[u], top[_next(u)]], np.c_[bottom[u], bottom[_next(u)]]
        other_upper = np.c_[top[_next(neighbor)], top[neighbor]]
        other_lower = np.c_[bottom[_next(neighbor)], bottom[neighbor]]
        exposed = ~shared | (upper != other_upper).any(axis=1) | (lower != other_lower).any(axis=1)
        arrays = (upper, lower, other_upper, other_lower)
        upper, lower, other_upper, other_lower = (a[exposed] for a in arrays)
        mid = lambda a: self.vertices[a, 2].sum(axis=1)
        z_top, z_bottom, other_top, other_bottom = map(mid, (upper, lower, other_upper, other_lower))
        shared = shared[exposed]
        a_hi, a_lo = self._difference(upper, lower, z_top, z_bottom, other_upper,
                                      other_lower, other_top, other_bottom, shared)
        b_hi, b_lo = self._difference(other_upper[shared], other_lower[shared],
                                      other_top[shared], other_bottom[shared], upper[shared],
                                      lower[shared], z_top[shared], z_bottom[shared], True)
        walls = []
        for (a, b), (c, d) in zip(np.r_[a_hi, b_hi[:, ::-1]], np.r_[a_lo, b_lo[:, ::-1]]):
            # Include all intermediate levels on each end of the wall so
            # adjacent walls do not meet at T junctions.
            walls.extend((b, i + 1, i) for i in range(c, a))
            walls.extend((c, i, i + 1) for i in range(d, b))
        return np.asarray(walls, dtype=int).reshape(-1, 3)

    def shell(self, upper, lower):
        top, bottom = self.index[[upper, lower]]
        has_material = (top != bottom).any(axis=1)
        floor = bottom[has_material, ::-1]
        if lower == 2:
            floor = self._rectangular_floor(bottom.ravel(), floor)
        return np.concatenate((top[has_material], floor, self._walls(top.ravel(), bottom.ravel())))

    def _rectangular_floor(self, bottom, default):
        boundary = self.left[self.right < 0]
        a, b = bottom[boundary], bottom[_next(boundary)]
        xy = self.vertices[:, :2]
        low, high = xy.min(axis=0), xy.max(axis=0)
        # Use a boundary fan only when the roof covers a complete rectangle.
        on_side = ((xy[a] == xy[b]) & ((xy[a] == low) | (xy[a] == high))).any(axis=1)
        edge_a, edge_b = xy[a] - low, xy[b] - low
        twice_area = (edge_a[:, 0] * edge_b[:, 1] - edge_a[:, 1] * edge_b[:, 0]).sum()
        if not on_side.all() or twice_area != 2 * np.prod(high - low):
            return default
        center = len(self.vertices) - 1
        return np.c_[b, a, np.full(len(a), center)]


def reconstruct_relief(rest, ways, bottom, *, clip_ways=False):
    """Return an indexed full, rest and ways surface on a shared arrangement."""
    xy, fields = _overlay(rest, ways, bottom, clip_ways)
    for attempt in range(6):
        roof = _Roof(xy, fields)
        if not np.array_equal(roof.vertices, roof.vertices.astype(np.float32)):
            raise ValueError("relief coordinates exceed exact STL precision")
        shells = tuple(roof.shell(a, b) for a, b in ((0, 2), (1, 2), (0, 1)))
        pinches = []
        for faces in shells:
            edges = np.sort(np.stack((faces, faces[:, [1, 2, 0]]), axis=-1).reshape(-1, 2), axis=1)
            edges, count = np.unique(edges, axis=0, return_counts=True)
            if (count == 1).any():
                raise ValueError("relief reconstruction has an open surface")
            pinches.extend(roof.vertices[edges[count > 2], :2].reshape(-1, 2))
        if not pinches:
            return roof.vertices, shells
        xy, fields = _separate_pinches(xy, fields, np.unique(pinches, axis=0), fill=attempt > 0)
        fields[0] = np.maximum(fields[0], fields[1])
    raise ValueError("relief has unresolved vertical edge contacts")
