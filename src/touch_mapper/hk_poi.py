import math
from collections import Counter
from pathlib import Path

import shapely
from defusedxml.ElementTree import iterparse
from manifold3d import Manifold, OpType
from shapely import Point, box
from shapely.prepared import prep

from .hkstp_landmarks import DIGIT_TOP_MM, GAP_MM, PLAQUE_BOTTOM_MM, PLAQUE_TOP_MM, _forms, _nearby, _numeral
from .osm import mercator_y
from .print_mesh import BASE_HEIGHT_MM


SOURCE = "OpenStreetMap"
ATTRIBUTION = "Points of interest © OpenStreetMap contributors, ODbL."
MAX_POIS = 99
MAX_PLACED = 30
# Higher-priority categories are numbered and placed first.
CATEGORIES = (
    ("station", "MTR station", lambda t: t.get("railway") == "station" and "MTR" in t.get("network", "")),
    ("hospital", "Hospital", lambda t: t.get("amenity") == "hospital"),
    ("market", "Market", lambda t: t.get("amenity") == "marketplace"),
    ("library", "Library", lambda t: t.get("amenity") == "library"),
    ("post_office", "Post office", lambda t: t.get("amenity") == "post_office"),
    ("police", "Police station", lambda t: t.get("amenity") == "police"),
    ("community", "Community centre", lambda t: t.get("amenity") == "community_centre"),
    ("sports", "Sports centre", lambda t: t.get("leisure") == "sports_centre"),
    ("park", "Park", lambda t: t.get("leisure") == "park"),
    ("mall", "Shopping centre", lambda t: t.get("shop") == "mall"),
    ("bus_station", "Bus terminus", lambda t: t.get("amenity") == "bus_station"),
)
LABELS = {key: label for key, label, _ in CATEGORIES}
# Dense districts have little open ground, so markers may also sit on flat roofs.
ROOF_PROBE_MM = 2.6
ROOF_SINK_MM = 0.25
# Underground stations are marked at a street exit when the station itself is built over.
EXIT_RADIUS_M = 300
# Stations use the rectangular plaque; every other POI the circular one.
FORMS = {"station": "entrance"}


def _category(tags):
    if not tags.get("name"):
        return None
    for key, _, matches in CATEGORIES:
        if matches(tags):
            return key
    return None


def load(path: Path):
    nodes, features = {}, []
    for _, element in iterparse(path):
        if element.tag not in {"node", "way"}:
            continue
        tags = {tag.get("k"): tag.get("v") for tag in element.findall("tag")}
        if element.tag == "node":
            point = float(element.get("lon")), float(element.get("lat"))
            nodes[element.get("id")] = point
            points = [point]
        else:
            points = [nodes[ref] for ref in (nd.get("ref") for nd in element.findall("nd")) if ref in nodes]
        kind = "exit" if tags.get("railway") == "subway_entrance" else _category(tags)
        if kind == "exit":
            features.append((kind, tags.get("ref", ""), points[0]))
        elif kind and points:
            anchor = tuple(sum(values) / len(points) for values in zip(*points))
            features.append((kind, " ".join(tags["name"].split()), anchor))
        element.clear()
    return features


def select(features, bounds):
    order = {key: index for index, (key, _, _) in enumerate(CATEGORIES)}
    exits = [anchor for kind, _, anchor in features if kind == "exit"]
    features = [feature for feature in features if feature[0] != "exit"]
    groups, skipped = {}, Counter()
    for kind, name, (lon, lat) in sorted(features, key=lambda item: (order[item[0]], item[1].casefold())):
        key = f"{kind}:{name}", FORMS.get(kind, "reception")
        if not (bounds.west < lon < bounds.east and bounds.south < lat < bounds.north):
            skipped["outsideMap"] += 1
        elif key in groups:
            skipped["duplicate"] += 1
        elif len(groups) >= MAX_POIS:
            skipped["overLimit"] += 1
        else:
            nearby = sorted((_metres((lon, lat), exit), exit) for exit in exits) if kind == "station" else []
            groups[key] = [(name, (lon, lat))] + [(name, exit) for d, exit in nearby if d < EXIT_RADIUS_M]
    return groups, skipped


def _metres(a, b):
    return math.hypot((a[0] - b[0]) * math.cos(math.radians(a[1])), a[1] - b[1]) * 111_320


def _region(section):
    polygons = []
    for part in section.decompose():
        rings = sorted(
            (shapely.Polygon(ring) for ring in part.to_polygons() if len(ring) >= 3),
            key=lambda ring: -ring.area,
        )
        if rings:
            polygons.append(shapely.Polygon(rings[0].exterior, [ring.exterior for ring in rings[1:]]))
    return prep(shapely.make_valid(shapely.MultiPolygon(polygons)))


def _outline(kind, number, xy):
    if kind == "entrance":
        half = (5.8 if number < 10 else 9.8) / 2 + GAP_MM, 7.2 / 2 + GAP_MM
        return box(xy[0] - half[0], xy[1] - half[1], xy[0] + half[0], xy[1] + half[1])
    return Point(xy).buffer((3.9 if number < 10 else 5.25) + GAP_MM, quad_segs=6)


def build_relief(groups, bounds, scale, rest, ways):
    width, height = (span * 1000 / scale for span in bounds._span_meters())
    south = mercator_y(bounds.south)
    y_span = mercator_y(bounds.north) - south

    def project(lon, lat):
        return (
            round((lon - bounds.west) / (bounds.east - bounds.west) * width, 2),
            round((mercator_y(lat) - south) / y_span * height, 2),
        )

    occupied = rest.trim_by_plane((0, 0, 1), BASE_HEIGHT_MM + 0.05).project() + ways.project()
    ground = _region(rest.slice(PLAQUE_BOTTOM_MM + 0.05) - occupied)
    buildings = rest.slice(ROOF_PROBE_MM)
    roofs = _region(buildings - ways.project())
    buildings = _region(buildings)
    plate = _region(rest.slice(PLAQUE_BOTTOM_MM + 0.05))
    taken, placed, solids = [], [], []

    def top_under(outline):
        x0, y0, x1, y1 = outline.bounds
        prism = Manifold.cube((x1 - x0, y1 - y0, 50)).translate((x0, y0, 0))
        return max((part ^ prism).bounding_box()[5] for part in (rest, ways))

    for (poi_id, kind), alternatives in groups.items():
        number = len(placed) + 1
        if number > MAX_PLACED:
            break
        anchors = [(name, anchor, project(*anchor)) for name, anchor in alternatives]
        candidates = (
            (name, anchor, projected, xy)
            for rank in zip(*(_nearby(projected) for _, _, projected in anchors))
            for (name, anchor, projected), xy in zip(anchors, rank)
        )
        spot = column = None
        least, cover = None, math.inf
        for name, anchor, projected, xy in candidates:
            outline = _outline(kind, number, xy)
            if any(outline.intersects(other) for other in taken) or not plate.contains(outline):
                continue
            here = xy, outline, anchor, projected
            if column is None and not buildings.intersects(outline):
                column = (*here, 0.0, "column")
            if kind == "entrance" and column is None:
                covered = buildings.context.intersection(outline).area
                if covered < cover:
                    least, cover = (*here, 0.0, "column"), covered
            if ground.contains(outline):
                spot = (*here, PLAQUE_BOTTOM_MM, "ground")
                break
            if roofs.contains(outline.buffer(-GAP_MM)):
                spot = (*here, top_under(outline) - ROOF_SINK_MM, "roof")
                break
        # No open space nearby: stand the plaque on a solid column over the roads.
        # A station must be marked, so its column may stand over the fewest buildings.
        spot = spot or column or least
        if spot is None:
            continue
        xy, outline, anchor, projected, bottom, mount = spot
        form = _forms(number)[kind]
        plaque_top = (
            top_under(outline) + PLAQUE_TOP_MM - BASE_HEIGHT_MM if mount == "column"
            else bottom + PLAQUE_TOP_MM - PLAQUE_BOTTOM_MM
        )
        digit_bottom = plaque_top - 0.15
        plaque = form.extrude(plaque_top - bottom).translate((*xy, bottom))
        digits = _numeral(number).extrude(DIGIT_TOP_MM - PLAQUE_TOP_MM + 0.15).translate((*xy, digit_bottom))
        solids.append(plaque + digits)
        taken.append(outline)
        placed.append({
            "number": number, "building": alternatives[0][0], "buildingId": poi_id, "kind": kind,
            "anchorLonLat": list(anchor), "printXYMm": list(xy), "shiftMm": round(math.dist(projected, xy), 2),
            "mount": mount,
        })
    return Manifold.batch_boolean(solids, OpType.Add), placed


def report(groups, skipped, placed):
    return {
        "status": "included" if placed else "no-pois",
        "source": SOURCE,
        "attribution": ATTRIBUTION,
        "candidateCount": len(groups),
        "placedCount": len(placed),
        "skipped": {**dict(skipped), "noClearPlacement": len(groups) - len(placed)},
        "pois": [{
            "number": item["number"], "name": item["building"], "category": item["buildingId"].split(":", 1)[0],
            "anchorLonLat": item["anchorLonLat"], "printXYMm": item["printXYMm"], "shiftMm": item["shiftMm"],
            "mount": item.get("mount", "ground"),
        } for item in placed],
    }


def legend(data):
    rows = [
        "# Points of interest", "",
        "Read the raised numbers north-up. Rectangle = MTR station; circle = other point of interest.",
        "Markers sit on open ground, a flat roof or a raised column, up to 15 mm from the place they name.",
        "", "| Number | Name | Type |", "| --- | --- | --- |",
    ]
    for item in data["pois"]:
        name = item["name"].replace("|", "\\|")
        rows.append(f"| {item['number']} | {name} | {LABELS[item['category']]} |")
    if not data["pois"]:
        rows += ["", "No points of interest were placed on this print."]
    return "\n".join([*rows, "", data["attribution"], ""])
