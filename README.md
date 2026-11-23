# py-touch-mapper

A Python CLI for making tactile maps from OpenStreetMap. It runs the original
[Touch Mapper](https://github.com/skarkkai/touch-mapper) converter locally: OSM2World
builds the geometry, the clipper trims it to the print area, Blender applies the
tactile relief, and the map description is generated from the same features.
There is no website, AWS account, or API key to configure.

## Quick start (Docker)

Build from this checkout. The image includes the pinned upstream engine, Java,
Node.js, Blender 2.78c, and the Python CLI. The image runs as Linux amd64; on
Apple Silicon, Docker must support x86-64 emulation.

```sh
docker build --platform linux/amd64 -t py-touch-mapper .
docker run --rm --platform linux/amd64 py-touch-mapper doctor
```

Print a rectangular map around a latitude and longitude. The output directory
must be new; Docker writes it to the mounted working directory. These shell
examples use POSIX syntax; on Linux, the user flag preserves file ownership.

```sh
docker run --rm --platform linux/amd64 \
  --user "$(id -u):$(id -g)" -v "$PWD:/work" py-touch-mapper \
  generate --center 22.2835 114.1585 --width-cm 12 --height-cm 10 \
  --scale 1800 --out-dir central-map
```

To render offline, provide an unedited OSM 0.6 XML extract that contains a
`<bounds>` element and versioned nodes, ways, and relations:

```sh
docker run --rm --platform linux/amd64 --network none \
  --user "$(id -u):$(id -g)" -v "$PWD:/work" py-touch-mapper \
  generate --osm tests/fixtures/mixed.osm --scale 2100 --out-dir sample-map
```

## Native Python installation

Use Python 3.10+ and a system Cairo library (e.g. `libcairo2` on Debian/Ubuntu).
The PyPI dependencies alone do **not** include the rendering engine. Check out
[the pinned Touch Mapper revision](https://github.com/skarkkai/touch-mapper/tree/e2710eac904ed5d0d17d918a132beb496cbf80d4),
build its modified OSM2World with Ant and JDK 17, and install Blender **2.78c**
with `svgwrite==1.1.9` and `pyparsing==2.4.7` in Blender's bundled Python 3.5.
Use Node.js 18+ and the upstream
[local runtime setup](https://github.com/skarkkai/touch-mapper/blob/e2710eac904ed5d0d17d918a132beb496cbf80d4/doc/development-setup.md)
for the `converter/OSM2World` and `converter/blender` links. The upstream AWS
worker and web dependencies are not needed. The Dockerfile shows the full
Linux setup.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
export TOUCH_MAPPER_RUNTIME=/absolute/path/to/touch-mapper
touch-mapper doctor --verbose
touch-mapper generate --osm tests/fixtures/mixed.osm --scale 2100 --out-dir sample-map
```

## Choosing map settings

| Setting | Use |
|---|---|
| `--center LAT LON` | Fetch a map centered on a point. Defaults to 17 × 17 cm and marks the center in the 3D model. |
| `--bbox WEST SOUTH EAST NORTH` | Fetch the specified bounds, in longitude/latitude order. |
| `--osm FILE` | Use the file's bounds and data without fetching anything. |
| `--scale N` | Set print scale 1:N (default 1:3100). For a fixed area, a larger N makes a smaller print. |
| `--width-cm`, `--height-cm` | With `--center`, set physical dimensions. Otherwise bounds and scale determine dimensions. |
| `--content-mode` | Choose `normal`, `no-buildings`, `only-big-roads`, or `only-named-roads`. |
| `--marker LAT LON` / `--hide-marker` | Mark an interior point in the 3D model, or omit the default center marker. The marker must fit inside the print edges. |

See `touch-mapper generate --help` for relief height overrides, border and
road density options. The map must be 1–99.9 cm on each axis. Bounds must not
cross the antimeridian. Online commands send the requested coordinates to
OpenStreetMap's public map API; choose a small area or use an offline extract
if the server rejects the request. For API use you can set
`TOUCH_MAPPER_OSM_CONTACT` to a contact URL or email included in the User-Agent
(pass the variable through with `docker run -e TOUCH_MAPPER_OSM_CONTACT …`).

## Outputs

| Output | Use |
|---|---|
| `map.stl` | One-piece 3D print in millimetres. |
| `map-ways.stl`, `map-rest.stl` | Aligned split parts for different materials or colours. An empty ways part is valid. |
| `map.svg`, `map.pdf` | North-up embossing artwork; includes extra space above the map for orientation. |
| `map.blend` | Editable Blender scene. |
| `map-content.json` | Structured description from the upstream map data pipeline. |
| `manifest.json` | Generated files, bounds, scale, print size, and OpenStreetMap attribution. |

On success, the CLI also prints the manifest as JSON to stdout; progress and
errors go to stderr. Keep [OpenStreetMap attribution](https://www.openstreetmap.org/copyright)
with maps you share. Verify print dimensions and legibility in your slicer or
embosser; the CLI does not generate printer-specific G-code.

## Agent skill

Copy the [`touch-mapper` skill](skills/touch-mapper/SKILL.md) folder to your
agent's skills directory (e.g. `.agents/skills/` for Codex or `.claude/skills/`
for Claude Code). Install the CLI or build the Docker image first.

## Tests and license

`python -m unittest discover -s tests -v` runs the offline unit tests after
installation. To include real artifact generation, also set
`TOUCH_MAPPER_RUNTIME` to a built engine and `TOUCH_MAPPER_TEST_ENGINE=1`.
The GitHub workflow runs both against the Docker image with networking disabled.

The CLI is licensed under [AGPL-3.0-only](LICENSE), like Touch Mapper.
OSM2World and Blender retain their own licenses. The offline fixture is from
the pinned Touch Mapper revision.