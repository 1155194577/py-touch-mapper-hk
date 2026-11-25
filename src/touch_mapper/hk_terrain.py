import io
import json
import math
import os
import sys
import tempfile
import time
import zipfile
import zlib
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np


DTM_SOURCE = "https://data.gov.hk/en-data/dataset/hk-landsd-openmap-5m-grid-dtm"
DTM_DOWNLOAD = "https://www.landsd.gov.hk/landsd_psi_data/SMO/data/Whole_HK_DTM_5m.zip"
DTM_MEMBER = "Whole_HK_DTM_5m.asc"
DTM_ATTRIBUTION = "Lands Department, Government of the Hong Kong Special Administrative Region — Digital Terrain Model (5 m grid)"

DISTRICTS_DATASET = "had_rcd_1634523272907_75218"
DISTRICTS_SOURCE = f"https://portal.csdi.gov.hk/csdi-webpage/dataset/{DISTRICTS_DATASET}"
DISTRICTS_SERVICE = f"https://portal.csdi.gov.hk/server/rest/services/common/{DISTRICTS_DATASET}/MapServer/0/query"
DISTRICTS_ATTRIBUTION = "Home Affairs Department and CSDI Portal — District Council Districts (Hong Kong)"
DISTRICTS_SIMPLIFY_M = 50
CSDI_TERMS = "https://portal.csdi.gov.hk/csdi-webpage/doc/TNC"

CACHE_AGE = 7 * 24 * 60 * 60
MAX_DTM_ZIP_BYTES = 64 * 1024 * 1024
MAX_DTM_ASC_BYTES = 512 * 1024 * 1024
MAX_DISTRICTS_BYTES = 4 * 1024 * 1024
HK1980_BOUNDS = (790_000.0, 790_000.0, 880_000.0, 860_000.0)
SEA_MARGIN_M = 2000.0


def _cache_dir():
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "touch-mapper"


def _write_cache(cache: Path, data: bytes):
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=cache.parent, delete=False) as staged:
        try:
            staged.write(data)
            staged.close()
            os.replace(staged.name, cache)
        finally:
            Path(staged.name).unlink(missing_ok=True)


def _read_bounded(stream, limit: int, label: str) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{label} exceeds its size limit")
    return data


def _read_header(stream):
    header = {}
    for _ in range(6):
        raw = stream.readline()
        if not raw:
            raise ValueError("incomplete DTM ASCII header")
        parts = raw.decode("ascii").split()
        if len(parts) != 2:
            raise ValueError("invalid DTM ASCII header line")
        key, value = parts[0].lower(), float(parts[1])
        if not math.isfinite(value):
            raise ValueError(f"non-finite DTM header value for {key}")
        header[key] = value
    required = {"ncols", "nrows", "xllcorner", "yllcorner", "cellsize", "nodata_value"}
    if not required.issubset(header):
        raise ValueError("DTM ASCII header is missing required fields")
    ncols, nrows = int(header["ncols"]), int(header["nrows"])
    cellsize = float(header["cellsize"])
    if ncols < 2 or nrows < 2 or cellsize <= 0:
        raise ValueError("DTM dimensions and cellsize must be positive")
    return ncols, nrows, float(header["xllcorner"]), float(header["yllcorner"]), cellsize, float(header["nodata_value"])


def parse_dtm_zip(data: bytes, step_m: int = 250):
    if len(data) > MAX_DTM_ZIP_BYTES:
        raise ValueError("Hong Kong DTM archive exceeds its size limit")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        member_name = DTM_MEMBER if DTM_MEMBER in archive.namelist() else next(
            (name for name in archive.namelist() if name.lower().endswith(".asc")), None
        )
        if member_name is None:
            raise ValueError("Hong Kong DTM archive does not contain an .asc grid")
        member = archive.getinfo(member_name)
        if member.file_size > MAX_DTM_ASC_BYTES:
            raise ValueError("Hong Kong DTM ASCII grid exceeds its size limit")
        with archive.open(member) as stream:
            ncols, nrows, xll, yll, cellsize, nodata = _read_header(stream)
            stride_float = step_m / cellsize
            stride = max(1, int(round(stride_float)))
            if abs(stride - stride_float) > 1e-6:
                raise ValueError(f"--step-m ({step_m}) must be a multiple of the DTM cell size ({cellsize:g} m)")
            rows = []
            row_indices = []
            for row_idx in range(nrows):
                line = stream.readline()
                if not line:
                    raise ValueError("unexpected end of DTM grid data")
                if row_idx % stride == 0:
                    values = np.fromstring(line, sep=" ", dtype=np.float32)
                    if len(values) != ncols:
                        raise ValueError(f"DTM row {row_idx} has {len(values)} columns, expected {ncols}")
                    rows.append(values[::stride])
                    row_indices.append(row_idx)

    if len(rows) < 2 or len(rows[0]) < 2:
        raise ValueError("DTM sampling step is too coarse for the grid")

    # Flip vertically so row 0 is South (minimum Y) and row -1 is North (maximum Y).
    grid = np.asarray(rows, dtype=np.float32)[::-1].copy()
    grid[~np.isfinite(grid) | (grid == nodata)] = 0.0

    xs = xll + (np.arange(0, ncols, stride, dtype=np.float64) + 0.5) * cellsize
    ys = yll + (nrows - 1 - np.asarray(row_indices[::-1], dtype=np.float64) + 0.5) * cellsize
    actual_step = stride * cellsize

    land = grid > 0
    if not np.any(land):
        raise ValueError("Hong Kong DTM contains no positive land elevation")
    ly, lx = np.where(land)
    pad = max(2, int(math.ceil(SEA_MARGIN_M / actual_step)))
    r0, r1 = max(0, int(ly.min()) - pad), min(grid.shape[0], int(ly.max()) + pad + 1)
    c0, c1 = max(0, int(lx.min()) - pad), min(grid.shape[1], int(lx.max()) + pad + 1)

    grid = grid[r0:r1, c0:c1]
    xs = xs[c0:c1]
    ys = ys[r0:r1]

    return {
        "grid": grid,
        "xs": xs,
        "ys": ys,
        "cellsize_m": cellsize,
        "step_m": actual_step,
        "max_elevation_m": float(grid.max()),
        "bounds_hk1980": [float(xs[0]), float(ys[0]), float(xs[-1]), float(ys[-1])],
    }


def load_dtm(step_m: int = 250):
    cache = _cache_dir() / "hk-dtm-5m.zip"
    invalid = (OSError, ValueError, zipfile.BadZipFile, zlib.error, KeyError, UnicodeDecodeError)
    cached_bytes = None
    try:
        age = time.time() - cache.stat().st_mtime
        with cache.open("rb") as source:
            cached_bytes = _read_bounded(source, MAX_DTM_ZIP_BYTES, "cached Hong Kong DTM")
        if age < CACHE_AGE:
            return parse_dtm_zip(cached_bytes, step_m=step_m), "cache"
    except invalid:
        cached_bytes = None

    try:
        request = Request(DTM_DOWNLOAD, headers={"User-Agent": "py-touch-mapper (HK terrain overview)"})
        with urlopen(request, timeout=60) as response:
            data = _read_bounded(response, MAX_DTM_ZIP_BYTES, "Hong Kong DTM download")
        dtm = parse_dtm_zip(data, step_m=step_m)
    except (*invalid, HTTPException) as exc:
        if cached_bytes is None:
            raise ValueError(f"Hong Kong DTM unavailable: {exc}") from exc
        print(f"Warning: Hong Kong DTM unavailable ({exc}); using cached DTM", file=sys.stderr)
        return parse_dtm_zip(cached_bytes, step_m=step_m), "stale-cache"

    try:
        _write_cache(cache, data)
    except OSError as exc:
        print(f"Warning: could not cache Hong Kong DTM ({exc})", file=sys.stderr)
    return dtm, "live"


def _valid_ring(ring):
    if not isinstance(ring, list) or len(ring) < 4:
        return False
    for point in ring:
        if (
            not isinstance(point, list)
            or len(point) < 2
            or not all(type(v) in (int, float) and math.isfinite(v) for v in point[:2])
            or not (HK1980_BOUNDS[0] <= point[0] <= HK1980_BOUNDS[2])
            or not (HK1980_BOUNDS[1] <= point[1] <= HK1980_BOUNDS[3])
        ):
            return False
    return True


def _validate_districts(data):
    if not isinstance(data, dict) or not isinstance(data.get("fetchedAt"), str):
        raise ValueError("invalid Hong Kong districts snapshot")
    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("no Hong Kong district records found")
    for feat in features:
        attrs = feat.get("attributes") if isinstance(feat, dict) else None
        geom = feat.get("geometry") if isinstance(feat, dict) else None
        rings = geom.get("rings") if isinstance(geom, dict) else None
        if not isinstance(attrs, dict) or not isinstance(attrs.get("NAME_EN"), str):
            raise ValueError("invalid Hong Kong district attributes")
        if not isinstance(rings, list) or not rings or not all(_valid_ring(ring) for ring in rings):
            raise ValueError("invalid Hong Kong district geometry in HK1980 Grid")
    return data


def _fetch_districts():
    query = urlencode({
        "where": "1=1",
        "outFields": "OBJECTID,AREA_CODE,NAME_EN,NAME_TC,REGION",
        "outSR": 2326,
        "maxAllowableOffset": DISTRICTS_SIMPLIFY_M,
        "orderByFields": "OBJECTID ASC",
        "f": "json",
    })
    request = Request(
        f"{DISTRICTS_SERVICE}?{query}",
        headers={"User-Agent": "py-touch-mapper (HK district boundaries)"},
    )
    with urlopen(request, timeout=20) as response:
        payload = json.loads(_read_bounded(response, MAX_DISTRICTS_BYTES, "CSDI districts response"))
    if not isinstance(payload, dict) or "error" in payload or not isinstance(payload.get("features"), list):
        raise ValueError("CSDI did not return Hong Kong district features")
    return {"fetchedAt": datetime.now(timezone.utc).isoformat(), "features": payload["features"]}


def load_districts():
    cache = _cache_dir() / "hk-districts-v1.json"
    cached = None
    try:
        age = time.time() - cache.stat().st_mtime
        with cache.open("rb") as source:
            cached = _validate_districts(json.loads(_read_bounded(source, MAX_DISTRICTS_BYTES, "cached districts")))
        if age < CACHE_AGE:
            return cached, "cache"
    except (OSError, ValueError):
        cached = None

    try:
        data = _validate_districts(_fetch_districts())
    except (OSError, ValueError, HTTPException) as exc:
        if cached is not None:
            print(f"Warning: HK district boundaries unavailable ({exc}); using cached data", file=sys.stderr)
            return cached, "stale-cache"
        print(f"Warning: HK district boundaries unavailable ({exc}); continuing without district ridges", file=sys.stderr)
        return None, "unavailable"

    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    try:
        _write_cache(cache, payload)
    except OSError as exc:
        print(f"Warning: could not cache HK district boundaries ({exc})", file=sys.stderr)
    return data, "live"
