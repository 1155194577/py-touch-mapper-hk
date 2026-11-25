import argparse
import json
import math
import os
import sys
import tempfile
from collections import Counter
from functools import partial
from pathlib import Path
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException

from . import hk_paving, hkstp
from .osm import ATTRIBUTION, Bounds, copy_osm, fetch_osm, read_bounds
from .print_mesh import finalize_stls
from .runtime import Runtime

MODES = ("normal", "no-buildings", "only-big-roads", "only-named-roads")
STL_ARTIFACTS = ("map.stl", "map-ways.stl", "map-rest.stl")
ARTIFACTS = (*STL_ARTIFACTS, "map.svg", "map.pdf", "map.blend", "map-content.json")


def number(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    return parsed


def parser():
    root = argparse.ArgumentParser(prog="touch-mapper", description="Make printable tactile maps from OpenStreetMap.")
    commands = root.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="create STL, SVG, PDF and map description files")
    doctor = commands.add_parser("doctor", help="check the local Touch Mapper engine")
    for command in (generate, doctor):
        command.add_argument("--runtime", type=Path, help="upstream checkout (default: TOUCH_MAPPER_RUNTIME)")
        command.add_argument("-v", "--verbose", action="store_true", help="show converter logs")
    sources = generate.add_mutually_exclusive_group(required=True)
    sources.add_argument("--osm", type=Path, metavar="FILE", help="local OSM 0.6 XML with bounds")
    sources.add_argument("--bbox", nargs=4, type=number, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    sources.add_argument("--center", nargs=2, type=number, metavar=("LAT", "LON"))
    generate.add_argument("-o", "--out-dir", required=True, type=Path, help="new directory for the finished map")
    generate.add_argument("--scale", type=int, default=3100, metavar="N", help="print scale 1:N (default: 3100)")
    generate.add_argument("--width-cm", type=number, help="print width for --center (default: 17)")
    generate.add_argument("--height-cm", type=number, help="print height for --center (default: 17)")
    generate.add_argument("--content-mode", choices=MODES, default="normal")
    generate.add_argument("--road-density", type=number, help="major road retention, 1–100 (default: 10)")
    generate.add_argument("--hk-tactile-paving", action="store_true", help="add Hong Kong tactile guide paths to STLs")
    generate.add_argument("--hk-poi", action="store_true", help="add numbered OpenStreetMap points of interest to STLs")
    generate.add_argument("--hkstp-landmarks", action="store_true", help="add numbered Science Park landmarks to STLs")
    generate.add_argument("--no-borders", action="store_true", help="omit the raised map borders")
    markers = generate.add_mutually_exclusive_group()
    markers.add_argument("--marker", nargs=2, type=number, metavar=("LAT", "LON"))
    markers.add_argument("--hide-marker", action="store_true", help="omit the default center marker")
    for kind in ("road", "path", "building", "railway"):
        generate.add_argument(f"--{kind}-height-mm", type=number, help=f"override {kind} relief; 0 omits this category")
    indoor = commands.add_parser("hkstp-floor", help="make a tactile STL of Green 18's ground floor from CSDI")
    floor_source = indoor.add_mutually_exclusive_group(required=True)
    floor_source.add_argument("--building", choices=(hkstp.BUILDING,))
    floor_source.add_argument("--floor-id", choices=(hkstp.FLOOR_ID,))
    indoor.add_argument("--floor", choices=(hkstp.FLOOR,), default=hkstp.FLOOR)
    indoor.add_argument("--size-mm", type=number, default=190, help="longest print side in mm (120–250, default: 190)")
    indoor.add_argument("-o", "--out-dir", type=Path, required=True, help="new directory for the floor map")
    overview = commands.add_parser("hk-overview", help="make a one-piece whole Hong Kong tactile terrain overview STL")
    overview.add_argument("--size-mm", type=number, default=210, help="longest print side in mm (120–250, default: 210)")
    overview.add_argument("--relief-mm", type=number, default=8.0, help="maximum terrain relief in mm (2–20, default: 8)")
    overview.add_argument("--step-m", type=int, default=250, help="DTM sampling step in metres, multiple of 5 (100–500, default: 250)")
    overview.add_argument("--no-districts", action="store_true", help="omit raised 18-district boundary ridges on land")
    overview.add_argument("--no-borders", action="store_true", help="omit the raised perimeter frame and north cue")
    overview.add_argument("-o", "--out-dir", type=Path, required=True, help="new directory for the Hong Kong overview map")
    return root


def validate(args):
    if not 1 <= args.scale <= 1_000_000:
        raise ValueError("scale must be between 1 and 1,000,000")
    if args.road_density is not None and (
        args.content_mode != "only-big-roads" or not 1 <= args.road_density <= 100
    ):
        raise ValueError("--road-density must be 1–100 and requires --content-mode only-big-roads")
    for kind in ("road", "path", "building", "railway"):
        height = getattr(args, f"{kind}_height_mm")
        if height is not None and not (0 <= height <= 50.8):
            raise ValueError(f"--{kind}-height-mm must be between 0 and 50.8")
    if args.content_mode == "no-buildings" and args.building_height_mm not in (None, 0):
        raise ValueError("--building-height-mm conflicts with --content-mode no-buildings")
    if args.center is None and (args.width_cm is not None or args.height_cm is not None):
        raise ValueError("--width-cm and --height-cm require --center; otherwise dimensions follow the bounds and scale")


def check_dimensions(dimensions):
    if not all(1 <= axis <= 99.9 for axis in dimensions):
        raise ValueError("print width and height must be 1–99.9 cm; adjust the area or scale")


def stage_input(args, osm: Path):
    if args.osm is not None:
        if not args.osm.is_file():
            raise ValueError(f"OSM file does not exist: {args.osm}")
        with args.osm.open("rb") as source:
            copy_osm(source, osm)
    else:
        if args.center:
            width = args.width_cm if args.width_cm is not None else 17
            height = args.height_cm if args.height_cm is not None else 17
            check_dimensions((width, height))
            bounds = Bounds.centered(*args.center, width, height, args.scale)
        else:
            bounds = Bounds(*args.bbox)
            check_dimensions(bounds.print_dimensions(args.scale))
        if args.marker:
            bounds.marker(*args.marker, args.scale)
        print("Downloading OpenStreetMap data", file=sys.stderr)
        fetch_osm(bounds, osm, os.environ.get("TOUCH_MAPPER_OSM_CONTACT"))
    return read_bounds(osm)


def generate(runtime: Runtime, args):
    output = args.out_dir.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError(f"output directory already exists: {output}")
    if not output.parent.is_dir():
        raise ValueError(f"output parent directory does not exist: {output.parent}")

    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".touch-mapper-") as workspace:
        work = Path(workspace)
        osm = work / "map.osm"
        bounds = stage_input(args, osm)
        dimensions = bounds.print_dimensions(args.scale)
        check_dimensions(dimensions)
        marker_point = None if args.hide_marker else args.marker or args.center
        marker = json.dumps(bounds.marker(*marker_point, args.scale)) if marker_point else None
        # Read POIs before road-only pruning drops their standalone nodes.
        poi_features = None
        if args.hk_poi:
            from . import hk_poi

            poi_features = hk_poi.load(osm)
        if args.content_mode in {"only-big-roads", "only-named-roads"}:
            runtime.prune(osm, bounds, dimensions, args)
        runtime.convert(osm, dimensions, marker, args)
        for filename in STL_ARTIFACTS:
            artifact = work / filename
            if not artifact.is_file() or not artifact.stat().st_size:
                raise ValueError(f"converter did not produce {filename}")
        paths = build_paving = None
        if args.hk_tactile_paving:
            paths = hk_paving.load_paths(bounds)
            if paths:
                build_paving = partial(hk_paving.paving_relief, paths, bounds, args.scale, has_borders=not args.no_borders)
        build_relief = build_paving
        legend = None
        if args.hkstp_landmarks:
            from . import hkstp_landmarks

            snapshot, source = None, "outside-campus"
            groups, skipped = {}, Counter()
            placed = []
            if hkstp_landmarks.intersects_campus(bounds):
                snapshot, source = hkstp.load_campus()
                groups, skipped = hkstp_landmarks.select(snapshot["layers"], bounds)
            if groups:
                def build_relief(rest, ways):
                    paving, dot_count = build_paving(rest, ways) if build_paving else (None, 0)
                    occupied = rest + paving if dot_count else rest
                    landmarks, items = hkstp_landmarks.build_relief(groups, bounds, args.scale, occupied, ways)
                    placed.extend(items)
                    combined = paving + landmarks if dot_count else landmarks
                    return combined, dot_count + len(items)

        pois = []
        if args.hk_poi:
            poi_groups, poi_skipped = hk_poi.select(poi_features, bounds)
            if poi_groups:
                build_inner = build_relief

                def build_relief(rest, ways):
                    inner, inner_count = build_inner(rest, ways) if build_inner else (None, 0)
                    occupied = rest + inner if inner_count else rest
                    markers, items = hk_poi.build_relief(poi_groups, bounds, args.scale, occupied, ways)
                    pois.extend(items)
                    return (inner + markers if inner_count else markers), inner_count + len(items)

        print("Preparing printable STL solids", file=sys.stderr, flush=True)
        relief_count = finalize_stls(
            work, blender_relief=True, has_borders=not args.no_borders, build_additional_relief=build_relief,
            blender_scale=args.scale,
            building_height_mm=0 if args.content_mode == "no-buildings" else args.building_height_mm,
        )
        if args.hkstp_landmarks:
            landmarks_report = hkstp_landmarks.report(source, snapshot, groups, skipped, placed)
            legend = hkstp_landmarks.legend(landmarks_report)
        runtime.describe(work)
        runtime.embossing_pdf(work)

        result = work / "result"
        result.mkdir()
        for filename in ARTIFACTS:
            artifact = work / filename
            if not artifact.is_file() or not artifact.stat().st_size:
                raise ValueError(f"converter did not produce {filename}")
            artifact.rename(result / filename)
        manifest = {
            "outputDir": str(output),
            "files": list(ARTIFACTS) + ["manifest.json"],
            "bbox": list(bounds.values),
            "scale": args.scale,
            "printWidthCm": dimensions[0],
            "printHeightCm": dimensions[1],
            "contentMode": args.content_mode,
            "attribution": ATTRIBUTION,
        }
        if args.hk_tactile_paving:
            dot_count = relief_count - len(pois) - (len(placed) if args.hkstp_landmarks else 0)
            status = "unavailable" if paths is None else "included" if dot_count else "no-data"
            manifest["hkTactilePaving"] = {
                "status": status,
                "dotCount": dot_count,
                "source": hk_paving.SOURCE,
                "attribution": hk_paving.ATTRIBUTION,
            }
        if legend is not None:
            legend_file = "hkstp-landmarks.md"
            (result / legend_file).write_text(legend, encoding="utf-8")
            manifest["files"].append(legend_file)
            manifest["hkstpLandmarks"] = landmarks_report
        if args.hk_poi:
            poi_report = hk_poi.report(poi_groups, poi_skipped, pois)
            (result / "points-of-interest.md").write_text(hk_poi.legend(poi_report), encoding="utf-8")
            manifest["files"].append("points-of-interest.md")
            manifest["hkPoi"] = poi_report
        (result / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if output.exists() or output.is_symlink():
            raise ValueError(f"output directory already exists: {output}")
        result.rename(output)
    return manifest


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "hkstp-floor":
            from .floor_mesh import generate_floor

            print(json.dumps(generate_floor(args.out_dir, args.size_mm)))
            return 0
        if args.command == "hk-overview":
            from .hk_overview_mesh import generate_overview

            print(json.dumps(generate_overview(
                args.out_dir,
                size_mm=args.size_mm,
                relief_mm=args.relief_mm,
                step_m=args.step_m,
                include_districts=not args.no_districts,
                has_borders=not args.no_borders,
            )))
            return 0
        if args.command == "generate":
            validate(args)
        runtime_path = args.runtime or os.environ.get("TOUCH_MAPPER_RUNTIME")
        if not runtime_path:
            raise ValueError("set TOUCH_MAPPER_RUNTIME or pass --runtime (see README for Docker or native setup)")
        runtime = Runtime(Path(runtime_path), args.verbose)
        if args.command == "doctor":
            runtime.check()
            print(f"Touch Mapper runtime ready: {runtime.path}")
        else:
            print(json.dumps(generate(runtime, args)))
    except (OSError, ValueError, ParseError, DefusedXmlException) as exc:
        print(f"touch-mapper: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("touch-mapper: interrupted", file=sys.stderr)
        return 130
    return 0
