import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

class Runtime:
    def __init__(self, path: Path, verbose: bool = False):
        self.path = path.resolve()
        self.verbose = verbose
        self.converter = self.path / "converter"
        self.blender = self.converter / "blender/blender"
        required = [
            self.converter / "osm-to-tactile.py",
            self.converter / "prune-only-big-roads.js",
            self.converter / "map_desc/map-description-classifications.json",
            self.converter / "OSM2World/build/OSM2World.jar",
            self.blender,
        ]
        if missing := [path for path in required if not path.is_file()]:
            raise ValueError("incomplete Touch Mapper runtime; see README for setup (missing "
                             + ", ".join(str(path) for path in missing) + ")")
        if missing_tools := [tool for tool in ("java", "node") if not shutil.which(tool)]:
            raise ValueError("install runtime executables: " + ", ".join(missing_tools))
        try:
            from cairosvg import svg2pdf
        except OSError as exc:
            raise ValueError("PDF export needs the system Cairo library (libcairo2 on Debian/Ubuntu)") from exc
        self.svg_to_pdf = svg2pdf

    def run(self, stage: str, cmd: list):
        print(stage, file=sys.stderr, flush=True)
        with tempfile.TemporaryFile() as log:
            result = subprocess.run(cmd, cwd=self.path, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode or self.verbose:
                if result.returncode and not self.verbose:
                    log.seek(max(0, log.tell() - 8192))
                else:
                    log.seek(0)
                for line in log:
                    print(line.decode("utf-8", errors="replace").rstrip(), file=sys.stderr)
            if result.returncode:
                raise ValueError(f"{stage} failed (exit {result.returncode})")

    def check(self):
        for stage, cmd in [
            ("Checking Java", ["java", "-version"]),
            ("Checking Node.js", ["node", "--version"]),
            ("Checking Blender", [self.blender, "--version"]),
        ]:
            self.run(stage, cmd)

    def prune(self, osm: Path, bounds, dimensions, args):
        cmd = [
            "node", self.converter / "prune-only-big-roads.js",
            "--content-mode", args.content_mode, "--osm", osm,
            "--lon-min", str(bounds.west), "--lat-min", str(bounds.south),
            "--lon-max", str(bounds.east), "--lat-max", str(bounds.north),
            "--print-width-cm", str(dimensions[0]), "--print-height-cm", str(dimensions[1]),
            "--map-scale", str(args.scale),
        ]
        if args.content_mode == "only-big-roads":
            cmd.extend(["--target-road-density", str((args.road_density or 10) / 10)])
        self.run("Filtering OSM features", cmd)

    def convert(self, osm: Path, dimensions, marker, args):
        cmd = [
            sys.executable, self.converter / "osm-to-tactile.py", osm,
            "--scale", str(args.scale),
            "--print-width-cm", str(dimensions[0]),
            "--print-height-cm", str(dimensions[1]),
        ]
        if marker is not None:
            cmd.extend(["--marker1", marker])
        if args.no_borders:
            cmd.append("--no-borders")
        for kind in ("road", "path", "building", "railway"):
            height = getattr(args, f"{kind}_height_mm")
            if kind == "building" and args.content_mode == "no-buildings":
                height = 0
            if height is not None:
                cmd.extend([f"--{kind}-height-mm", str(height)])
        self.run("Converting OSM to tactile geometry", cmd)

    def describe(self, work_dir: Path):
        self.run("Building the map description", [
            sys.executable, "-m", "converter.map_desc", work_dir / "map-meta-raw.json",
        ])

    def embossing_pdf(self, work_dir: Path):
        self.svg_to_pdf(url=str(work_dir / "map.svg"), write_to=str(work_dir / "map.pdf"))
