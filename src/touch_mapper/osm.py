import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from defusedxml.ElementTree import iterparse

EARTH_CIRCUMFERENCE_M = 40075016.686
OSM_API = "https://api.openstreetmap.org/api/0.6/map"
OSM_MAX_AREA = 0.25
MAX_OSM_BYTES = 25 * 1024 * 1024
ATTRIBUTION = "© OpenStreetMap contributors — https://www.openstreetmap.org/copyright"


@dataclass(frozen=True)
class Bounds:
    west: float
    south: float
    east: float
    north: float

    def __post_init__(self):
        if not (
            -180 <= self.west < self.east <= 180
            and -85 <= self.south < self.north <= 85
        ):
            raise ValueError(
                "bounds must be west < east and south < north within "
                "±180° longitude and ±85° latitude (no antimeridian crossing)"
            )

    @property
    def values(self):
        return self.west, self.south, self.east, self.north

    @classmethod
    def centered(cls, lat, lon, width_cm, height_cm, scale):
        if not (-85 <= lat <= 85 and -180 <= lon <= 180):
            raise ValueError("center is outside ±85° latitude or ±180° longitude")
        half_lat = height_cm * scale / 200 * 360 / EARTH_CIRCUMFERENCE_M
        half_lon = width_cm * scale / 200 * 360 / (
            EARTH_CIRCUMFERENCE_M * math.cos(math.radians(lat))
        )
        return cls(lon - half_lon, lat - half_lat, lon + half_lon, lat + half_lat)

    def print_dimensions(self, scale):
        # Match OSM2World's latitude-adjusted Mercator projection.
        def mercator_y(lat):
            return math.asinh(math.tan(math.radians(lat))) / (2 * math.pi)

        meters = EARTH_CIRCUMFERENCE_M * math.cos(math.radians((self.south + self.north) / 2))
        width_cm = (self.east - self.west) * meters / 360 * 100 / scale
        height_cm = (mercator_y(self.north) - mercator_y(self.south)) * meters * 100 / scale
        return round(width_cm, 2), round(height_cm, 2)

    def marker(self, lat, lon):
        x = (lon - self.west) / (self.east - self.west)
        y = (lat - self.south) / (self.north - self.south)
        if not (0.04 < x < 0.96 and 0.04 < y < 0.96):
            raise ValueError("marker must be at least 4% in from each map edge")
        return {"x": x, "y": y}


def read_bounds(path: Path) -> Bounds:
    bounds = root = None
    depth = 0
    with path.open("rb") as source:
        for event, element in iterparse(source, events=("start", "end"), forbid_dtd=True):
            if event == "start":
                if depth == 0:
                    root = element
                    if root.tag != "osm" or root.get("version") != "0.6":
                        raise ValueError("input must be an OSM 0.6 XML extract")
                depth += 1
                continue

            depth -= 1
            if depth != 1:
                continue
            if element.tag == "bounds":
                if bounds is not None:
                    raise ValueError("OSM XML must contain exactly one <bounds> element")
                try:
                    values = [float(element.attrib[key]) for key in ("minlon", "minlat", "maxlon", "maxlat")]
                except (KeyError, ValueError):
                    raise ValueError("OSM <bounds> must have numeric minlon, minlat, maxlon and maxlat") from None
                bounds = Bounds(*values)
            elif element.tag in {"node", "way", "relation"}:
                if "action" in element.attrib or not element.get("version"):
                    raise ValueError("use an unedited OSM API extract with versioned nodes, ways and relations")
            root.remove(element)
    if bounds is None:
        raise ValueError("OSM XML needs a <bounds> element to set the print area")
    return bounds


def copy_osm(source, destination: Path):
    size = 0
    with destination.open("wb") as output:
        while chunk := source.read(64 * 1024):
            size += len(chunk)
            if size > MAX_OSM_BYTES:
                raise ValueError("OSM data exceeds 25 MiB; extract a smaller area")
            output.write(chunk)


def fetch_osm(bounds: Bounds, destination: Path, contact: str | None):
    if (bounds.east - bounds.west) * (bounds.north - bounds.south) > OSM_MAX_AREA:
        raise ValueError("OpenStreetMap API area exceeds 0.25 square degrees; choose a smaller area")
    bbox = ",".join(str(coord) for coord in bounds.values)
    agent = "py-touch-mapper/0.1 (+https://github.com/1155194577/py-touch-mapper)"
    if contact:
        agent += f" {contact}"
    request = urllib.request.Request(f"{OSM_API}?bbox={bbox}", headers={"User-Agent": agent})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            copy_osm(response, destination)
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 413}:
            raise ValueError("OpenStreetMap rejected the area; try a smaller bounding box") from exc
        raise ValueError(f"OpenStreetMap download failed (HTTP {exc.code}); try again later or use --osm") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"OpenStreetMap download failed: {exc.reason}; try again later or use --osm") from exc
