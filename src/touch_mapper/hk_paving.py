import io
import math
import os
import sys
import tempfile
import time
import zipfile
import zlib
from http.client import HTTPException
from pathlib import Path
from urllib.request import Request, urlopen
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from manifold3d import Manifold, OpType

from .osm import mercator_y


SOURCE = "https://data.gov.hk/en-data/dataset/hk-td-tis_16-traffic-aids-drawings-v2/resource/2e2bbdac-c47e-4a4d-a40f-908639878bbe"
DOWNLOAD = "https://static.data.gov.hk/td/traffic-aids-drawings-v2/DTAD_TG_PATH_LINE.kmz"
ATTRIBUTION = "Hong Kong Transport Department — Traffic Aids Drawings (2nd generation), Tactile Guide Path Line"
CACHE_AGE = 7 * 24 * 60 * 60
MAX_KMZ_BYTES = 2 * 1024 * 1024
MAX_KML_BYTES = 24 * 1024 * 1024
KML_NS = "{http://www.opengis.net/kml/2.2}"
DOT_RADIUS_MM = 0.85
DOT_SPACING_MM = 2.8
DOT_TOP_MM = 2.8
DOT_BOTTOM_MM = 0.2


def _parse_kmz(data):
    if len(data) > MAX_KMZ_BYTES:
        raise ValueError("tactile guide path archive is too large")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        member = archive.getinfo("doc.kml")
        if member.file_size > MAX_KML_BYTES:
            raise ValueError("tactile guide path KML is too large")
        root = fromstring(archive.read(member), forbid_dtd=True)
    if root.tag != f"{KML_NS}kml":
        raise ValueError("unexpected tactile guide path KML")
    lines = []
    for coords in root.iterfind(f".//{KML_NS}LineString/{KML_NS}coordinates"):
        points = []
        for coord in (coords.text or "").split():
            lon, lat, *_ = coord.split(",")
            point = float(lon), float(lat)
            if not (-180 <= point[0] <= 180 and -85 <= point[1] <= 85):
                raise ValueError("invalid tactile guide path coordinate")
            if not points or points[-1] != point:
                points.append(point)
        if len(points) >= 2:
            lines.append(points)
    return lines


def _write_cache(cache, data):
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=cache.parent, delete=False) as staged:
        try:
            staged.write(data)
            staged.close()
            os.replace(staged.name, cache)
        finally:
            Path(staged.name).unlink(missing_ok=True)


def load_paths(bounds):
    if bounds.east < 113.7 or bounds.west > 114.5 or bounds.north < 22.1 or bounds.south > 22.6:
        return []
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    cache = base / "touch-mapper" / "hk-tactile-guide-path.kmz"
    invalid_data = (OSError, ValueError, ParseError, DefusedXmlException, KeyError, zipfile.BadZipFile, zlib.error)
    cached_paths = None
    try:
        age = time.time() - cache.stat().st_mtime
        with cache.open("rb") as source:
            cached_paths = _parse_kmz(source.read(MAX_KMZ_BYTES + 1))
        if age < CACHE_AGE:
            return cached_paths
    except invalid_data:
        pass

    try:
        request = Request(DOWNLOAD, headers={"User-Agent": "py-touch-mapper (HK tactile paving)"})
        with urlopen(request, timeout=15) as response:
            data = response.read(MAX_KMZ_BYTES + 1)
        paths = _parse_kmz(data)
    except (*invalid_data, HTTPException) as exc:
        fallback = "using cached data" if cached_paths is not None else "continuing without the layer"
        print(f"Warning: HK tactile paving unavailable ({exc}); {fallback}", file=sys.stderr)
        return cached_paths

    try:
        _write_cache(cache, data)
    except OSError as exc:
        print(f"Warning: could not cache HK tactile paving ({exc})", file=sys.stderr)
    return paths


def _clip_segment(start, end, width, height):
    low, high = 0.0, 1.0
    for a, b, maximum in zip(start, end, (width, height)):
        if a == b:
            if not DOT_RADIUS_MM <= a <= maximum - DOT_RADIUS_MM:
                return None
            continue
        enter, leave = sorted(((DOT_RADIUS_MM - a) / (b - a), (maximum - DOT_RADIUS_MM - a) / (b - a)))
        low, high = max(low, enter), min(high, leave)
        if low >= high:
            return None
    first = start if low == 0 else _point_at(start, end, low)
    last = end if high == 1 else _point_at(start, end, high)
    return first, last


def _point_at(start, end, fraction):
    return tuple(a + (b - a) * fraction for a, b in zip(start, end))


def _runs(points, width, height):
    run = []
    for a, b in zip(points, points[1:]):
        clipped = _clip_segment(a, b, width, height)
        if run and (clipped is None or run[-1] != clipped[0]):
            yield run
            run = []
        if clipped is not None:
            if not run:
                run.append(clipped[0])
            run.append(clipped[1])
    if run:
        yield run


def _dot_centers(points):
    segments = [(a, b, math.dist(a, b)) for a, b in zip(points, points[1:]) if a != b]
    length = sum(distance for _, _, distance in segments)
    count = max(1, math.ceil(length / DOT_SPACING_MM))
    start = 0
    index = 0
    for a, b, distance in segments:
        while index < count and (position := (index + 0.5) * length / count) <= start + distance:
            yield _point_at(a, b, (position - start) / distance)
            index += 1
        start += distance


def paving_relief(paths, bounds, scale):
    width, height = (span * 1000 / scale for span in bounds._span_meters())
    south = mercator_y(bounds.south)
    lon_span = bounds.east - bounds.west
    y_span = mercator_y(bounds.north) - south
    centers = set()
    for line in paths:
        points = [
            ((lon - bounds.west) / lon_span * width, (mercator_y(lat) - south) / y_span * height)
            for lon, lat in line
        ]
        for run in _runs(points, width, height):
            centers.update(_dot_centers(run))

    dot = Manifold.cylinder(DOT_TOP_MM - DOT_BOTTOM_MM, DOT_RADIUS_MM, circular_segments=12)
    relief = Manifold.batch_boolean(
        [dot.translate((x, y, DOT_BOTTOM_MM)) for x, y in sorted(centers)], OpType.Add
    )
    return relief, len(centers)
