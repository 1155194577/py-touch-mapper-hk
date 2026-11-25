import json
import tempfile
from pathlib import Path

import numpy as np
import shapely
from manifold3d import Manifold, Mesh
from shapely.errors import GEOSException
from shapely.geometry import Polygon
from shapely.ops import unary_union

from . import hk_terrain
from .print_mesh import _solid_facets, _write_stl


# Binary STL stores float32 vertices, which represent this grid exactly.
GRID_MM = 1 / 64
BASE_MM = 1.6
COAST_STEP_MM = 1.0
DEFAULT_SIZE_MM = 210.0
DEFAULT_RELIEF_MM = 8.0
DEFAULT_STEP_M = 250
DISTRICT_RIDGE_MM = 0.7
BORDER_MM = 2.8
NORTH_CUE_MM = 3.8
FILES = ("map.stl", "legend.md", "manifest.json")

REGION_LABELS = {
    "HK": "Hong Kong Island",
    "KLN": "Kowloon",
    "NT": "New Territories",
}


def _district_border_mask(districts, xs, ys, land, step_m):
    if districts is None:
        return np.zeros_like(land)
    polys = []
    for feat in districts["features"]:
        rings = feat["geometry"]["rings"]
        parts = [Polygon(ring) for ring in rings if len(ring) >= 4]
        if parts:
            polys.append(unary_union(parts).buffer(0))
    if len(polys) < 2:
        return np.zeros_like(land)

    ly, lx = np.where(land)
    points = shapely.points(xs[lx], ys[ly])
    radius = step_m * 0.7
    counts = np.zeros(len(points), dtype=np.int16)
    for poly in polys:
        buffered = poly.buffer(radius)
        shapely.prepare(buffered)
        counts += shapely.contains(buffered, points)

    # A cell lies on an internal district boundary only where at least two districts meet.
    hits = counts >= 2
    mask = np.zeros_like(land)
    mask[ly[hits], lx[hits]] = True

    # Restrict district ridges to interior land so coastlines stay a clean single step.
    eroded = np.zeros_like(land)
    if land.shape[0] >= 3 and land.shape[1] >= 3:
        eroded[1:-1, 1:-1] = (
            land[1:-1, 1:-1]
            & land[:-2, 1:-1]
            & land[2:, 1:-1]
            & land[1:-1, :-2]
            & land[1:-1, 2:]
        )
    return mask & eroded


def _heightfield_solid(gx, gy, z):
    rows, cols = z.shape
    xx, yy = np.meshgrid(gx, gy)
    top_verts = np.column_stack([xx.ravel(), yy.ravel(), z.ravel()])

    # Perimeter indices counter-clockwise from +Z: bottom, right, top, left.
    p_bot = np.arange(0, cols - 1, dtype=np.uint32)
    p_right = np.arange(0, rows - 1, dtype=np.uint32) * cols + (cols - 1)
    p_top = (rows - 1) * cols + np.arange(cols - 1, 0, -1, dtype=np.uint32)
    p_left = np.arange(rows - 1, 0, -1, dtype=np.uint32) * cols
    perim_top = np.concatenate([p_bot, p_right, p_top, p_left])
    n_perim = len(perim_top)

    perim_bot_verts = top_verts[perim_top].copy()
    perim_bot_verts[:, 2] = 0.0
    center_bot = np.array([[gx[-1] / 2.0, gy[-1] / 2.0, 0.0]], dtype=np.float64)

    verts = np.vstack([top_verts, perim_bot_verts, center_bot]).astype(np.float32)
    bot_start = rows * cols
    center_idx = bot_start + n_perim

    r_idx = np.arange(rows - 1, dtype=np.uint32)[:, None]
    c_idx = np.arange(cols - 1, dtype=np.uint32)[None, :]
    v00 = (r_idx * cols + c_idx).ravel()
    v10 = v00 + 1
    v01 = v00 + cols
    v11 = v01 + 1
    top_tris = np.vstack([
        np.column_stack([v00, v10, v11]),
        np.column_stack([v00, v11, v01]),
    ])

    i = np.arange(n_perim, dtype=np.uint32)
    i_next = (i + 1) % n_perim
    t0, t1 = perim_top[i], perim_top[i_next]
    b0, b1 = bot_start + i, bot_start + i_next
    side_tris = np.vstack([
        np.column_stack([t0, b0, b1]),
        np.column_stack([t0, b1, t1]),
    ])
    bot_tris = np.column_stack([np.full(n_perim, center_idx, dtype=np.uint32), b1, b0])

    tris = np.vstack([top_tris, side_tris, bot_tris]).astype(np.uint32)
    return Manifold(Mesh(vert_properties=verts, tri_verts=tris))


def _build(dtm, districts, size_mm, relief_mm, has_borders=True):
    grid = dtm["grid"]
    xs = dtm["xs"]
    ys = dtm["ys"]
    span_x = float(xs[-1] - xs[0])
    span_y = float(ys[-1] - ys[0])
    if span_x <= 0 or span_y <= 0:
        raise ValueError("Hong Kong DTM has no printable horizontal extent")

    scale_mm_per_m = size_mm / max(span_x, span_y)
    land = grid > 0
    max_elev = max(float(grid.max()), 1.0)

    z = np.full(grid.shape, BASE_MM, dtype=np.float64)
    z[land] = BASE_MM + COAST_STEP_MM + (np.clip(grid[land], 0.0, None) / max_elev) * relief_mm

    district_cells = 0
    if districts is not None:
        try:
            border_mask = _district_border_mask(districts, xs, ys, land, dtm["step_m"])
        except GEOSException as exc:
            raise ValueError(f"cannot process Hong Kong district boundaries: {exc}") from exc
        district_cells = int(border_mask.sum())
        if district_cells:
            z[border_mask] += DISTRICT_RIDGE_MM

    rows, cols = z.shape
    if has_borders and rows >= 6 and cols >= 12:
        frame = max(1, min(2, int(round(1.5 / (dtm["step_m"] * scale_mm_per_m)))))
        z[:frame, :] = np.maximum(z[:frame, :], BORDER_MM)
        z[-frame:, :] = np.maximum(z[-frame:, :], BORDER_MM)
        z[:, :frame] = np.maximum(z[:, :frame], BORDER_MM)
        z[:, -frame:] = np.maximum(z[:, -frame:], BORDER_MM)
        # Centered tactile north-up bar on the top edge.
        half_cue = max(2, int(round(4.5 / (dtm["step_m"] * scale_mm_per_m))))
        mid = cols // 2
        cue_depth = min(rows - 1, frame + 1)
        z[-cue_depth:, max(0, mid - half_cue):min(cols, mid + half_cue + 1)] = np.maximum(
            z[-cue_depth:, max(0, mid - half_cue):min(cols, mid + half_cue + 1)],
            NORTH_CUE_MM,
        )

    gx = np.round((xs - xs[0]) * scale_mm_per_m / GRID_MM) * GRID_MM
    gy = np.round((ys - ys[0]) * scale_mm_per_m / GRID_MM) * GRID_MM
    z = np.round(z / GRID_MM) * GRID_MM

    solid = _heightfield_solid(gx, gy, z)
    facets = _solid_facets(FILES[0], solid, single_piece=True)
    x0, y0, z0, x1, y1, z1 = solid.bounding_box()
    dimensions = [round(value, 2) for value in (x1 - x0, y1 - y0, z1 - z0)]
    horizontal_scale = int(round(1000.0 / scale_mm_per_m))
    vertical_exaggeration = round((relief_mm / max_elev) / scale_mm_per_m, 2)
    return facets, dimensions, horizontal_scale, vertical_exaggeration, district_cells


def _legend(manifest):
    width, height, depth = manifest["printSizeMm"]
    scale = manifest["scale"]
    m_per_mm = round(scale / 1000)
    relief = manifest["reliefMm"]
    max_elev = round(manifest["maxElevationM"])
    exag = manifest["verticalExaggeration"]
    rows = [
        "# Hong Kong — Tactile Terrain Overview",
        "",
        f"Print at 100%: **{width:g} × {height:g} × {depth:g} mm** (fits a 220 × 220 mm bed). North is at the top.",
        f"Horizontal scale: **1:{scale:,}** (~{m_per_mm} m per mm). "
        f"Vertical terrain relief: **{relief:g} mm** for 0–{max_elev} m (~{exag:g}× vertical exaggeration).",
        "",
        "| Raised feature | Height | Meaning |",
        "| --- | --- | --- |",
        f"| Flat recessed base | {BASE_MM:g} mm | Sea and harbour waters (Victoria Harbour, Tolo Harbour, South China Sea) |",
        f"| Crisp step above sea | +{COAST_STEP_MM:g} mm | Hong Kong coastline and islands |",
        f"| Smooth terrain relief | +{relief:g} mm max | Mountains and valleys (Tai Mo Shan, Lantau Peak, Victoria Peak, Lion Rock) |",
    ]
    if manifest["districts"]["included"]:
        rows.append(
            f"| Narrow ridge on land | +{DISTRICT_RIDGE_MM:g} mm | Internal boundaries between the 18 District Council districts |"
        )
    if manifest["hasBorders"]:
        rows.append(f"| Outer frame | {BORDER_MM:g} mm | Map perimeter border |")
        rows.append(f"| Raised top-center bar | {NORTH_CUE_MM:g} mm | North-up orientation cue |")

    districts = manifest["districts"].get("names", [])
    if districts:
        rows += ["", "## 18 Districts", ""]
        by_region = {}
        for item in districts:
            region = REGION_LABELS.get(item.get("region"), item.get("region") or "Other")
            by_region.setdefault(region, []).append(item["nameEn"])
        for region, names in by_region.items():
            rows.append(f"- **{region}**: {', '.join(sorted(names))}")

    rows += [
        "",
        "This one-piece territory overview shows coastline, topography and district layout; "
        "individual streets and buildings require close-up tiled prints.",
        "",
        f"- {hk_terrain.DTM_ATTRIBUTION} ({hk_terrain.DTM_SOURCE})",
    ]
    if manifest["districts"]["included"]:
        rows.append(f"- {hk_terrain.DISTRICTS_ATTRIBUTION} ({hk_terrain.DISTRICTS_SOURCE})")
        rows.append(f"- CSDI Terms: {hk_terrain.CSDI_TERMS}")
    rows.append("")
    return "\n".join(rows)


def generate_overview(
    out_dir,
    size_mm=DEFAULT_SIZE_MM,
    relief_mm=DEFAULT_RELIEF_MM,
    step_m=DEFAULT_STEP_M,
    include_districts=True,
    has_borders=True,
):
    if not 120 <= size_mm <= 250:
        raise ValueError("--size-mm must be between 120 and 250")
    if not 2 <= relief_mm <= 20:
        raise ValueError("--relief-mm must be between 2 and 20")
    if not 100 <= step_m <= 500 or step_m % 5 != 0:
        raise ValueError("--step-m must be a multiple of 5 between 100 and 500")

    output = out_dir.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError(f"output directory already exists: {output}")
    if not output.parent.is_dir():
        raise ValueError(f"output parent directory does not exist: {output.parent}")

    dtm, dtm_source = hk_terrain.load_dtm(step_m=step_m)
    districts, districts_source = (hk_terrain.load_districts() if include_districts else (None, "disabled"))

    facets, dimensions, scale, exaggeration, district_cells = _build(
        dtm, districts, size_mm=size_mm, relief_mm=relief_mm, has_borders=has_borders
    )

    district_names = []
    if districts is not None:
        for feat in districts["features"]:
            attrs = feat["attributes"]
            district_names.append({
                "code": attrs.get("AREA_CODE"),
                "nameEn": attrs.get("NAME_EN"),
                "nameTc": attrs.get("NAME_TC"),
                "region": attrs.get("REGION"),
            })

    manifest = {
        "outputDir": str(output),
        "files": list(FILES),
        "region": "Hong Kong SAR",
        "printSizeMm": dimensions,
        "scale": scale,
        "reliefMm": relief_mm,
        "stepM": int(dtm["step_m"]),
        "maxElevationM": round(dtm["max_elevation_m"], 2),
        "verticalExaggeration": exaggeration,
        "hasBorders": bool(has_borders),
        "boundsHk1980": dtm["bounds_hk1980"],
        "triangleCount": len(facets),
        "terrain": {
            "source": dtm_source,
            "dataset": hk_terrain.DTM_SOURCE,
            "attribution": hk_terrain.DTM_ATTRIBUTION,
        },
        "districts": {
            "included": bool(districts is not None and district_cells > 0),
            "source": districts_source,
            "dataset": hk_terrain.DISTRICTS_SOURCE,
            "count": len(district_names),
            "ridgeCells": district_cells,
            "names": district_names,
            "attribution": hk_terrain.DISTRICTS_ATTRIBUTION,
            "terms": hk_terrain.CSDI_TERMS,
        },
    }

    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".touch-mapper-") as workspace:
        result = Path(workspace) / "result"
        result.mkdir()
        _write_stl(result / FILES[0], facets)
        (result / FILES[1]).write_text(_legend(manifest), encoding="utf-8")
        (result / FILES[2]).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if output.exists() or output.is_symlink():
            raise ValueError(f"output directory already exists: {output}")
        result.rename(output)
    return manifest
