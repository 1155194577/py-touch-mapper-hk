import json
import tempfile
from pathlib import Path

import numpy as np
from manifold3d import CrossSection, FillRule, Manifold, OpType
from shapely import Point, Polygon, box, make_valid, union_all
from shapely.affinity import scale, translate
from shapely.errors import GEOSException
from shapely.geometry import shape
from shapely.ops import linemerge, nearest_points, transform

from . import hkstp
from .osm import EARTH_CIRCUMFERENCE_M, mercator_y
from .print_mesh import _solid_facets, _write_stl


GRID_MM = 0.02
SIMPLIFY_MM = 0.14
BASE_MM = 1.8
HEIGHTS = {"units": 2.4, "corridors": 2.85, "border": 3.05, "landmarks": 3.7}
SYMBOLS = {
    "entrance": ("Triangle", "Principal doorway"),
    "elevator": ("Square", "Lift"),
    "guestservices": ("Circle", "Visitor services"),
    "restroom.wheelchair": ("Cross", "Wheelchair restroom"),
}
FILES = ("map.stl", "legend.md", "manifest.json")


def _parts(geometry, kind):
    if geometry.is_empty:
        return
    if geometry.geom_type == kind:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _parts(part, kind)


def _read_geometry(feature):
    try:
        return shape(feature["geometry"])
    except (GEOSException, KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        raise ValueError("CSDI contained invalid floor geometry") from exc


def _projector(floor, size_mm):
    west, south, east, north = floor.bounds
    if west == east or south == north:
        raise ValueError("CSDI floor has no printable area")
    width = (east - west) / 360
    min_y = mercator_y(south)
    height = mercator_y(north) - min_y
    factor = size_mm / max(width, height)
    if max(width, height) * EARTH_CIRCUMFERENCE_M > 350:
        raise ValueError("CSDI footprint is too large for a single building floor")

    def project(geometry):
        return transform(lambda x, y, *_: ((x - west) * factor / 360, (mercator_y(y) - min_y) * factor), geometry)

    return project


def _clean(geometry):
    polygons = tuple(_parts(make_valid(geometry), "Polygon"))
    return union_all(polygons, grid_size=GRID_MM)


def _symbol(kind, point):
    if kind == "entrance":
        geometry = Polygon(((-2.4, -1.8), (2.4, -1.8), (0, 2.6)))
    elif kind == "elevator":
        geometry = box(-1.7, -1.7, 1.7, 1.7)
    elif kind == "guestservices":
        geometry = Point(0, 0).buffer(2, quad_segs=12)
    else:
        geometry = box(-0.8, -2.2, 0.8, 2.2).union(box(-2.2, -0.8, 2.2, 0.8))
    return translate(geometry, point.x, point.y)


def _relief(layers, size_mm):
    footprint = _read_geometry(layers["floor"][0])
    project = _projector(footprint, size_mm)
    base = _clean(project(footprint).simplify(SIMPLIFY_MM))
    if base.geom_type != "Polygon" or base.is_empty:
        raise ValueError("CSDI floor does not form one connected footprint")
    border = base.difference(base.buffer(-1.25))
    interior = base.buffer(-1.65)
    if interior.is_empty:
        raise ValueError("CSDI floor is too narrow for the raised border")

    units = [(row["properties"].get("RESTRICTED"), _clean(project(_read_geometry(row)))) for row in layers["units"]]
    restricted = _clean(union_all([unit for access, unit in units if access != "N"]))
    allowed = interior.difference(restricted.buffer(0.15))
    corridors = []
    for row in layers["network"]:
        props = row["properties"]
        if props.get("RESTRICTED") == "N" and props.get("HIGHWAY") == "footway":
            corridors.extend(_parts(project(_read_geometry(row)), "LineString"))
    lines = union_all(corridors).simplify(SIMPLIFY_MM).buffer(0.55, quad_segs=4).intersection(allowed)
    walls = union_all([unit.boundary for access, unit in units if access == "N" and not unit.is_empty])
    walls = walls.simplify(SIMPLIFY_MM).buffer(0.4, quad_segs=3).intersection(allowed).difference(lines.buffer(0.35))

    gate_area = base.buffer(-4.35)
    if gate_area.is_empty:
        raise ValueError("CSDI floor cannot fit a distinct entrance marker")
    if len(layers["gates"]) != 1 or layers["gates"][0]["properties"].get("GATE") != "pedestrian.principal":
        raise ValueError("Green 18 GF does not have one principal doorway")
    gate = project(_read_geometry(layers["gates"][0]))
    if gate.geom_type != "LineString":
        gate = linemerge(gate)
    if gate.geom_type != "LineString" or not gate.length:
        raise ValueError("CSDI principal doorway is not one connected line")
    doorway = gate.interpolate(0.5, normalized=True)
    entrance = nearest_points(gate_area, doorway)[0]
    if entrance.distance(doorway) > 12:
        raise ValueError("CSDI principal doorway is too far from the printable floor")
    markers = [_symbol("entrance", entrance)]
    landmarks = [{"kind": "entrance", "printXYMm": [round(entrance.x, 2), round(entrance.y, 2)]}]
    for row in layers["landmarks"]:
        props = row["properties"]
        kind = props.get("PLACE")
        if kind not in hkstp.LANDMARKS:
            continue
        for point in _parts(project(_read_geometry(row)), "Point"):
            marker = _symbol(kind, point)
            if kind == "restroom.wheelchair" and allowed.covers(point):
                for factor in (0.9, 0.8, 0.7, 0.6):
                    if allowed.covers(marker):
                        break
                    marker = scale(_symbol(kind, point), factor, factor, origin=point)
            if not allowed.covers(marker) or any(marker.distance(old) < 0.9 for old in markers):
                continue
            markers.append(marker)
            landmarks.append({
                "kind": kind,
                "name": props.get("NAMEEN"),
                "printXYMm": [round(point.x, 2), round(point.y, 2)],
            })
    if {item["kind"] for item in landmarks} != SYMBOLS.keys():
        raise ValueError("CSDI floor could not fit all required landmark types; try a larger print")
    symbols = union_all(markers)
    gap = symbols.buffer(0.65)
    return base, {
        "units": walls.difference(gap),
        "corridors": lines.difference(gap),
        "border": border,
        "landmarks": symbols,
    }, landmarks


def _extrude(geometry, lower, upper):
    paths = []
    for polygon in _parts(_clean(geometry), "Polygon"):
        paths.extend(np.asarray(ring.coords)[:-1] for ring in (polygon.exterior, *polygon.interiors))
    return CrossSection(paths, FillRule.EvenOdd).extrude(upper - lower).translate((0, 0, lower))


def _build(snapshot, size_mm):
    try:
        base, relief, landmarks = _relief(snapshot["layers"], size_mm)
        parts = [_extrude(base, 0, BASE_MM)]
        for kind, geometry in relief.items():
            if not geometry.is_empty:
                parts.append(_extrude(geometry, BASE_MM - 0.15, HEIGHTS[kind]))
        solid = Manifold.batch_boolean(parts, OpType.Add)
        facets = _solid_facets(FILES[0], solid, single_piece=True)
    except GEOSException as exc:
        raise ValueError(f"cannot make HKSTP floor geometry printable: {exc}") from exc
    west, south, bottom, east, north, top = solid.bounding_box()
    dimensions = [round(value, 2) for value in (east - west, north - south, top - bottom)]
    return facets, dimensions, landmarks


def _legend(manifest):
    width, height, depth = manifest["printSizeMm"]
    rows = [
        "# Green 18 — ground floor", "",
        f"Print at 100%: **{width:g} × {height:g} × {depth:g} mm**. The plan is north-up.", "",
        "| Raised feature | Meaning | Count |", "| --- | --- | --- |",
        "| Thick outline | Floor perimeter | |",
        "| Low outline | Mapped non-restricted unit | |",
        "| Raised line | Mapped indoor footway, not physical tactile paving | |",
    ]
    for kind, (symbol, label) in SYMBOLS.items():
        count = sum(item["kind"] == kind for item in manifest["landmarks"])
        rows.append(f"| {symbol} | {label} | {count} |")
    rows += [
        "", "Read the triangle as a doorway location, not as a direction arrow. Restricted rooms are not outlined.",
        "The map is an orientation aid, not a verified accessible route or emergency plan. Confirm public access, "
        "connectivity and landmark locations with Science Park staff before relying on it.", "",
        hkstp.ATTRIBUTION, f"Terms: {hkstp.TERMS}", "",
    ]
    return "\n".join(rows)


def generate_floor(out_dir, size_mm):
    if not 120 <= size_mm <= 250:
        raise ValueError("--size-mm must be between 120 and 250")
    output = out_dir.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError(f"output directory already exists: {output}")
    if not output.parent.is_dir():
        raise ValueError(f"output parent directory does not exist: {output.parent}")
    snapshot, source = hkstp.load_floor()
    facets, dimensions, landmarks = _build(snapshot, size_mm)
    manifest = {
        "outputDir": str(output),
        "files": list(FILES),
        "building": hkstp.BUILDING,
        "floor": hkstp.FLOOR,
        "floorId": hkstp.FLOOR_ID,
        "printSizeMm": dimensions,
        "landmarks": landmarks,
        "triangleCount": len(facets),
        "attribution": hkstp.ATTRIBUTION,
        "csdi": {
            "source": source,
            "fetchedAt": snapshot["fetchedAt"],
            "terms": hkstp.TERMS,
            "datasets": {key: hkstp.CATALOGUE + value for key, value in hkstp.DATASETS.items()},
            "sourceRecordCounts": {key: len(rows) for key, rows in snapshot["layers"].items()},
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
