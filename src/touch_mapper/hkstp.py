import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BUILDING = "Green 18"
FLOOR = "GF"
FLOOR_ID = "F3977632070T2010082402000000"
BUILDING_ID = "3977632070T20100824"
NETWORK_ID = "F3977632070T201008240200"
DATASETS = {
    "floor": "itc_rcd_1732154638960_53979",
    "units": "itc_rcd_1732154914834_47869",
    "gates": "itc_rcd_1732155849963_42635",
    "network": "itc_rcd_1732156053383_65893",
    "landmarks": "itc_rcd_1732155153876_17820",
}
CATALOGUE = "https://portal.csdi.gov.hk/csdi-webpage/dataset/"
SERVICE = "https://portal.csdi.gov.hk/server/rest/services/common"
ATTRIBUTION = (
    "Source: Government of the Hong Kong Special Administrative Region and CSDI Portal. "
    "Intellectual property rights in the data belong to the Government and "
    "Hong Kong Science & Technology Parks Corporation, as applicable."
)
TERMS = "https://portal.csdi.gov.hk/csdi-webpage/doc/TNC"
LANDMARKS = ("elevator", "guestservices", "restroom.wheelchair")
CACHE_AGE = 7 * 24 * 60 * 60
PAGE_SIZE = 128
MAX_PAGES = 8
MAX_PAGE_BYTES = 1024 * 1024
MAX_CACHE_BYTES = 8 * 1024 * 1024
QUERIES = {
    "floor": (f"FLPOLYID LIKE '{NETWORK_ID}%'", "BLDGCSUID,ENGDISFLNM,RESTRICTED"),
    "units": (f"FLPOLYID='{FLOOR_ID}'", "RESTRICTED"),
    "gates": (f"FLPOLYID='{FLOOR_ID}' AND GATE='pedestrian.principal'", "GATE"),
    "network": (f"FLPOLYID='{NETWORK_ID}' AND RESTRICTED='N'", "RESTRICTED,HIGHWAY"),
    "landmarks": (
        f"FLPOLYID='{FLOOR_ID}' AND PLACE IN ({','.join(repr(kind) for kind in LANDMARKS)})",
        "PLACE,NAMEEN",
    ),
}
GEOMETRIES = {
    "floor": {"Polygon": 2, "MultiPolygon": 3},
    "units": {"Polygon": 2, "MultiPolygon": 3},
    "gates": {"LineString": 1, "MultiLineString": 2},
    "network": {"LineString": 1, "MultiLineString": 2},
    "landmarks": {"Point": 0, "MultiPoint": 1},
}


def _read_json(stream, limit):
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("CSDI response or cache exceeds its size limit")
    return json.loads(data)


def _query(layer):
    predicate, fields = QUERIES[layer]
    features = []
    object_id = -1
    for _ in range(MAX_PAGES):
        args = urlencode({
            "where": f"({predicate}) AND OBJECTID>{object_id}",
            "f": "geojson",
            "outSR": 4326,
            "outFields": f"OBJECTID,FLPOLYID,{fields}",
            "orderByFields": "OBJECTID ASC",
            "resultRecordCount": PAGE_SIZE,
            "returnZ": "false",
        })
        request = Request(
            f"{SERVICE}/{DATASETS[layer]}/MapServer/0/query?{args}",
            headers={"User-Agent": "py-touch-mapper (HKSTP floor map)"},
        )
        with urlopen(request, timeout=15) as response:
            page = _read_json(response, MAX_PAGE_BYTES)
        if not isinstance(page, dict) or page.get("type") != "FeatureCollection" or "error" in page:
            raise ValueError(f"CSDI did not return {layer} GeoJSON")
        rows = page.get("features")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            raise ValueError(f"invalid CSDI {layer} page")
        for row in rows:
            props = row.get("properties") if isinstance(row, dict) else None
            current = props.get("OBJECTID") if isinstance(props, dict) else None
            if type(current) is not int or current <= object_id:
                raise ValueError(f"CSDI {layer} page is not ordered by object ID")
            object_id = current
        features.extend(rows)
        if not page.get("exceededTransferLimit"):
            return features
        if not rows:
            raise ValueError(f"CSDI {layer} pagination did not advance")
    raise ValueError(f"CSDI {layer} query exceeded the page limit")


def _valid_coords(coords, depth):
    if not isinstance(coords, list) or not coords:
        return False
    if depth:
        return all(_valid_coords(child, depth - 1) for child in coords)
    return (
        2 <= len(coords) <= 3
        and all(type(value) in (int, float) and math.isfinite(value) for value in coords)
        and 114.205 <= coords[0] <= 114.215
        and 22.421 <= coords[1] <= 22.430
    )


def _validate_snapshot(data):
    fetched = data.get("fetchedAt") if isinstance(data, dict) else None
    layers = data.get("layers") if isinstance(data, dict) else None
    if not isinstance(fetched, str) or not isinstance(layers, dict) or layers.keys() != DATASETS.keys():
        raise ValueError("invalid HKSTP floor snapshot")
    for layer, rows in layers.items():
        if not isinstance(rows, list) or not rows or len(rows) > MAX_PAGES * PAGE_SIZE:
            raise ValueError(f"no usable HKSTP {layer} records")
        for row in rows:
            props = row.get("properties") if isinstance(row, dict) else None
            geometry = row.get("geometry") if isinstance(row, dict) else None
            if not isinstance(props, dict) or not isinstance(geometry, dict):
                raise ValueError(f"invalid HKSTP {layer} feature")
            geom_type = geometry.get("type")
            depths = GEOMETRIES[layer]
            if not isinstance(geom_type, str) or geom_type not in depths or not _valid_coords(
                geometry.get("coordinates"), depths[geom_type]
            ):
                raise ValueError(f"invalid HKSTP {layer} geometry in WGS84")
            key = props.get("FLPOLYID")
            if key != (NETWORK_ID if layer == "network" else FLOOR_ID):
                raise ValueError("HKSTP network prefix does not resolve uniquely to Green 18 GF")
    floor = layers["floor"][0]["properties"]
    if len(layers["floor"]) != 1 or (
        floor.get("BLDGCSUID"), floor.get("ENGDISFLNM"), floor.get("RESTRICTED")
    ) != (BUILDING_ID, FLOOR, "N"):
        raise ValueError("Green 18 GF is ambiguous or not marked public in CSDI")
    return data


def load_floor():
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    cache = base / "touch-mapper" / f"hkstp-v1-{FLOOR_ID}.json"
    cached = None
    try:
        age = time.time() - cache.stat().st_mtime
        with cache.open("rb") as source:
            cached = _validate_snapshot(_read_json(source, MAX_CACHE_BYTES))
        if age < CACHE_AGE:
            return cached, "cache"
    except (OSError, ValueError):
        pass

    try:
        layers = {layer: _query(layer) for layer in DATASETS}
    except (OSError, ValueError, HTTPException) as exc:
        if cached is None:
            raise ValueError(f"HKSTP CSDI data unavailable: {exc}") from exc
        print(f"Warning: HKSTP CSDI unavailable ({exc}); using cached floor data", file=sys.stderr)
        return cached, "stale-cache"
    data = _validate_snapshot({"fetchedAt": datetime.now(timezone.utc).isoformat(), "layers": layers})
    payload = json.dumps(data, separators=(",", ":")).encode()
    if len(payload) > MAX_CACHE_BYTES:
        raise ValueError("HKSTP floor snapshot exceeds its size limit")
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache.parent, delete=False) as staged:
            try:
                staged.write(payload)
                staged.close()
                os.replace(staged.name, cache)
            finally:
                Path(staged.name).unlink(missing_ok=True)
    except OSError as exc:
        print(f"Warning: could not cache HKSTP data ({exc})", file=sys.stderr)
    return data, "live"
