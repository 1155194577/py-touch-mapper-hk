from collections import Counter, defaultdict
from math import cos, dist, pi, sin

from manifold3d import CrossSection, Manifold, OpType
from shapely import Point, make_valid
from shapely.geometry import shape
from shapely.ops import nearest_points

from . import hkstp
from .floor_mesh import _parts
from .osm import mercator_y
from .print_mesh import BASE_HEIGHT_MM


KINDS = {"entrance": "Principal entrance", "reception": "Reception / visitor services (indoors)"}
LANDSD_ATTRIBUTION = "Building names: Lands Department, HKSAR Government, via CSDI Portal."
DIGITS = ("abcdef", "bc", "abged", "abgcd", "fgbc", "afgcd", "afgecd", "abc", "abcdefg", "abfgcd")
SEGMENTS = {
    "a": (2.9, 0.85, 0, 2.1), "b": (0.85, 2.2, 1.1, 1.05),
    "c": (0.85, 2.2, 1.1, -1.05), "d": (2.9, 0.85, 0, -2.1),
    "e": (0.85, 2.2, -1.1, -1.05), "f": (0.85, 2.2, -1.1, 1.05),
    "g": (2.9, 0.85, 0, 0),
}
MAX_SHIFT_MM = 15
GAP_MM = 0.9
PLAQUE_BOTTOM_MM = 0.35
PLAQUE_TOP_MM = 1.65
DIGIT_TOP_MM = 2.55


def intersects_campus(bounds):
    west, south, east, north = hkstp.CAMPUS_BOUNDS
    return bounds.west < east and west < bounds.east and bounds.south < north and south < bounds.north


def _coordinates(geometry, kind):
    if kind == "reception":
        return _parts(geometry, "Point")
    return (line.interpolate(0.5, normalized=True) for line in _parts(geometry, "LineString") if line.length)


def select(layers, bounds):
    floors = defaultdict(list)
    units = defaultdict(list)
    restricted = defaultdict(list)
    names = defaultdict(set)
    for row in layers["floor"]:
        floors[row["properties"].get("FLPOLYID")].append(row)
    for row in layers["units"]:
        props = row["properties"]
        units[props.get("FLPOLYID"), props.get("UNPOLYID")].append(props.get("RESTRICTED"))
    for row in layers["restricted"]:
        restricted[row["properties"].get("FLPOLYID")].append(make_valid(shape(row["geometry"])))
    for row in layers["buildings"]:
        props = row["properties"]
        name = props.get("BuildingNameEN") or props.get("BuildingNameTC")
        if isinstance(name, str) and name.strip():
            names[props.get("BuildingCSUID")].add(" ".join(name.split()))

    groups = defaultdict(list)
    skipped = Counter()
    for layer, kind, field, code in (
        ("gates", "entrance", "GATE", "pedestrian.principal"),
        ("landmarks", "reception", "PLACE", "guestservices"),
    ):
        for row in layers[layer]:
            props = row["properties"]
            key = props.get("FLPOLYID")
            matches = floors.get(key, ())
            if (
                props.get(field) != code or len(matches) != 1
                or matches[0]["properties"].get("ENGDISFLNM") != "GF"
            ):
                skipped["notConfirmedGroundFloor"] += 1
                continue
            floor = matches[0]
            access = units.get((key, props.get("UNPOLYID")), ())
            if (
                floor["properties"].get("RESTRICTED") != "N"
                or any(value != "N" for value in access)
                or (kind == "entrance" and not access)
            ):
                skipped["restrictedOrUnknownAccess"] += 1
                continue
            building_id = floor["properties"].get("BLDGCSUID")
            if not isinstance(building_id, str) or not names[building_id]:
                skipped["noBuildingName"] += 1
                continue
            building = min(names[building_id])
            geometry = make_valid(shape(floor["geometry"]))
            parts = tuple(_parts(geometry, "Polygon"))
            for point in _coordinates(shape(row["geometry"]), kind):
                if not (bounds.west < point.x < bounds.east and bounds.south < point.y < bounds.north):
                    skipped["outsideMap"] += 1
                    continue
                if kind == "entrance":
                    if not any(part.exterior.distance(point) <= 0.00004 for part in parts):
                        skipped["notNearBuildingExterior"] += 1
                        continue
                    anchor = point
                else:
                    if any(unit.covers(point) for unit in restricted[key]):
                        skipped["restrictedOrUnknownAccess"] += 1
                        continue
                    edges = [part.exterior for part in parts if part.covers(point)]
                    if not edges:
                        skipped["outsideBuilding"] += 1
                        continue
                    anchor = nearest_points(min(edges, key=lambda edge: edge.distance(point)), point)[0]
                groups[building_id, kind].append((building, (anchor.x, anchor.y)))
    return groups, skipped


def _numeral(number):
    text = str(number)
    result = CrossSection()
    for index, digit in enumerate(text):
        offset = (index - (len(text) - 1) / 2) * 4.1
        for segment in DIGITS[int(digit)]:
            width, height, x, y = SEGMENTS[segment]
            result += CrossSection.square((width, height), center=True).translate((x + offset, y))
    return result


def _nearby(anchor):
    yield anchor
    for radius in range(1, MAX_SHIFT_MM + 1):
        for i in range(16):
            angle = i * pi / 8
            yield (round(anchor[0] + radius * cos(angle), 2), round(anchor[1] + radius * sin(angle), 2))


def _forms(number):
    return {
        "entrance": CrossSection.square((5.8 if number < 10 else 9.8, 7.2), center=True),
        "reception": CrossSection.circle(3.9 if number < 10 else 5.25, circular_segments=24),
    }


def build_relief(groups, bounds, scale, rest, ways):
    width, height = (span * 1000 / scale for span in bounds._span_meters())
    south = mercator_y(bounds.south)
    y_span = mercator_y(bounds.north) - south

    def project(lon, lat):
        return (
            round((lon - bounds.west) / (bounds.east - bounds.west) * width, 2),
            round((mercator_y(lat) - south) / y_span * height, 2),
        )

    base = rest.slice(PLAQUE_BOTTOM_MM + 0.05)
    occupied = rest.slice(BASE_HEIGHT_MM + 0.05) + ways.project()
    clear = base - occupied
    placed = []
    solids = []
    for (building_id, kind), alternatives in sorted(groups.items(), key=lambda item: (
        item[1][0][0].casefold(), item[0][0], item[0][1],
    )):
        number = len(placed) + 1
        if number > 99:
            break
        form = _forms(number)[kind]
        safe = form.offset(GAP_MM)
        choices = sorted(set(alternatives))
        anchors = [(building, anchor, project(*anchor)) for building, anchor in choices]
        candidates = (
            (building, anchor, projected, xy)
            for rank in zip(*(_nearby(anchor) for _, _, anchor in anchors))
            for (building, anchor, projected), xy in zip(anchors, rank)
        )
        for building, anchor, projected, xy in candidates:
            if not (safe.translate(xy) - clear).is_empty():
                continue
            plaque = form.extrude(PLAQUE_TOP_MM - PLAQUE_BOTTOM_MM).translate((*xy, PLAQUE_BOTTOM_MM))
            digit_bottom = PLAQUE_TOP_MM - 0.15
            digits = _numeral(number).extrude(DIGIT_TOP_MM - digit_bottom).translate((*xy, digit_bottom))
            solids.append(plaque + digits)
            clear -= safe.translate(xy)
            placed.append({
                "number": number, "building": building, "buildingId": building_id, "kind": kind,
                "anchorLonLat": list(anchor), "printXYMm": list(xy), "shiftMm": round(dist(projected, xy), 2),
            })
            break
    return Manifold.batch_boolean(solids, OpType.Add), placed


def report(source, snapshot, groups, skipped, placed):
    return {
        "status": "included" if placed else "no-landmarks",
        "source": source,
        "fetchedAt": snapshot["fetchedAt"] if snapshot else None,
        "attribution": [hkstp.ATTRIBUTION, LANDSD_ATTRIBUTION],
        "terms": hkstp.TERMS,
        "datasets": {key: hkstp.CATALOGUE + dataset for key, dataset in hkstp.CAMPUS_DATASETS.items()},
        "candidateCount": len(groups),
        "placedCount": len(placed),
        "skipped": {
            **dict(skipped),
            "clustered": sum(len(rows) - 1 for rows in groups.values()),
            "noClearPlacement": len(groups) - len(placed),
        },
        "landmarks": placed,
    }


def legend(data):
    rows = [
        "# Science Park — campus landmarks", "",
        "Read the raised numbers north-up. Rectangle = principal entrance; circle = indoor reception.",
        "One marker per building and type. Markers may be shifted onto nearby open space for legibility.",
        "A reception marker identifies the building, not the desk’s exact position or an outdoor destination.",
        "Confirm access, opening hours and accessible routes with Science Park staff before relying on this map.",
        "", "| Number | Building | Landmark |", "| --- | --- | --- |",
    ]
    for item in data["landmarks"]:
        building = item["building"].replace("|", "\\|")
        rows.append(f"| {item['number']} | {building} | {KINDS[item['kind']]} |")
    if not data["landmarks"]:
        rows += ["", "No campus landmarks were placed on this print."]
    return "\n".join([*rows, "", *data["attribution"], f"Terms: {data['terms']}", ""])
