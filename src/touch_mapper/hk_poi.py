from collections import Counter
from pathlib import Path

from defusedxml.ElementTree import iterparse

from . import hkstp_landmarks


SOURCE = "OpenStreetMap"
ATTRIBUTION = "Points of interest © OpenStreetMap contributors, ODbL."
MAX_POIS = 99
# Higher-priority categories are numbered and placed first.
CATEGORIES = (
    ("station", "MTR station", lambda t: t.get("railway") == "station" and "MTR" in t.get("network", "")),
    ("hospital", "Hospital", lambda t: t.get("amenity") == "hospital"),
    ("market", "Market", lambda t: t.get("amenity") == "marketplace"),
    ("library", "Library", lambda t: t.get("amenity") == "library"),
    ("post_office", "Post office", lambda t: t.get("amenity") == "post_office"),
    ("police", "Police station", lambda t: t.get("amenity") == "police"),
    ("community", "Community centre", lambda t: t.get("amenity") == "community_centre"),
    ("sports", "Sports centre", lambda t: t.get("leisure") in {"sports_centre", "swimming_pool"}),
    ("park", "Park", lambda t: t.get("leisure") == "park"),
    ("mall", "Shopping centre", lambda t: t.get("shop") == "mall"),
    ("bus_station", "Bus terminus", lambda t: t.get("amenity") == "bus_station"),
)
LABELS = {key: label for key, label, _ in CATEGORIES}
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
        kind = _category(tags)
        if kind and points:
            anchor = tuple(sum(values) / len(points) for values in zip(*points))
            features.append((kind, " ".join(tags["name"].split()), anchor))
        element.clear()
    return features


def select(features, bounds):
    order = {key: index for index, (key, _, _) in enumerate(CATEGORIES)}
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
            groups[key] = [(name, (lon, lat))]
    return groups, skipped


def build_relief(groups, bounds, scale, rest, ways):
    return hkstp_landmarks.build_relief(groups, bounds, scale, rest, ways)


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
        } for item in placed],
    }


def legend(data):
    rows = [
        "# Points of interest", "",
        "Read the raised numbers north-up. Rectangle = MTR station; circle = other point of interest.",
        "Markers sit on open ground and may be shifted up to 15 mm from the place they name.",
        "", "| Number | Name | Type |", "| --- | --- | --- |",
    ]
    for item in data["pois"]:
        name = item["name"].replace("|", "\\|")
        rows.append(f"| {item['number']} | {name} | {LABELS[item['category']]} |")
    if not data["pois"]:
        rows += ["", "No points of interest were placed on this print."]
    return "\n".join([*rows, "", data["attribution"], ""])
