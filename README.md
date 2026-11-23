# py-touch-mapper

A Python CLI for making tactile maps from OpenStreetMap. It runs the original
[Touch Mapper](https://github.com/skarkkai/touch-mapper) converter locally: OSM2World
builds the geometry, the clipper trims it to the print area, and Blender applies
the tactile relief. A final STL step closes local seams and fuses the relief to
the base without remeshing the map. The map description uses the same features.
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
| `--osm FILE` | Use the file's bounds and OSM data without fetching from OpenStreetMap. |
| `--scale N` | Set print scale 1:N (default 1:3100). For a fixed area, a larger N makes a smaller print. |
| `--width-cm`, `--height-cm` | With `--center`, set physical dimensions. Otherwise bounds and scale determine dimensions. |
| `--content-mode` | Choose `normal`, `no-buildings`, `only-big-roads`, or `only-named-roads`. |
| `--hk-tactile-paving` | Add Hong Kong Transport Department tactile guide paths to the STL models. Off by default. |
| `--marker LAT LON` / `--hide-marker` | Mark an interior point in the 3D model, or omit the default center marker. The marker must fit inside the print edges. |

See `touch-mapper generate --help` for relief height overrides, border and
road density options. The map must be 1–99.9 cm on each axis. Bounds must not
cross the antimeridian. Online commands send the requested coordinates to
OpenStreetMap's public map API; choose a small area or use an offline extract
if the server rejects the request. For API use you can set
`TOUCH_MAPPER_OSM_CONTACT` to a contact URL or email included in the User-Agent
(pass the variable through with `docker run -e TOUCH_MAPPER_OSM_CONTACT …`).

### Hong Kong tactile guide paths

Add `--hk-tactile-paving` to a Hong Kong map to include the Transport
Department's [Tactile Guide Path Line](https://data.gov.hk/en-data/dataset/hk-td-tis_16-traffic-aids-drawings-v2/resource/2e2bbdac-c47e-4a4d-a40f-908639878bbe)
from DATA.GOV.HK. The CLI downloads the territory-wide KMZ (it does not send
your bounds to DATA.GOV.HK) and clips the guide paths to the map. The layer
can make a network request even with `--osm`; omit the flag for offline use.
The download is cached for seven days under `$XDG_CACHE_HOME/touch-mapper`
(default `~/.cache/touch-mapper`). If the download fails, the CLI warns and
uses the last valid cache if available, or makes the map without the layer.

Guide paths appear as rows of round 1.7 mm-wide raised dots on `map.stl`
and `map-rest.stl`. The dot tops are 2.8 mm above the print bed, compared
with about 1.4 mm for default roads. This is a map symbol, not a
scale reproduction of the paving. Use a close-up map to distinguish short
paths. This source does not include every guide path, and the flag does not
include the separate tactile warning-stripe dataset. The layer is STL-only;
it does not change the SVG, PDF, Blender scene or map description. When
requested, `manifest.json` reports whether it was included, had no data in
the printable area, or was unavailable, and includes the source and attribution.

## Outputs

| Output | Use |
|---|---|
| `map.stl` | One connected solid with a flat base at Z=0, in millimetres. |
| `map-ways.stl`, `map-rest.stl` | Import together as parts of one object for a multi-material print. Keep their shared origin; don't drop each part onto the bed. Skip the ways file if it has no triangles. |
| `map.svg`, `map.pdf` | North-up embossing artwork; includes extra space above the map for orientation. |
| `map.blend` | Editable Blender scene. |
| `map-content.json` | Structured description from the upstream map data pipeline. |
| `manifest.json` | Generated files, bounds, scale, print size, and OpenStreetMap attribution. |

On success, the CLI also prints the manifest as JSON to stdout; progress and
errors go to stderr. Keep [OpenStreetMap attribution](https://www.openstreetmap.org/copyright)
with maps you share. The CLI resolves overlaps between the split parts and
checks the STL geometry before publishing any files. It can close small
converter seams, extend relief feet that nearly touch the base, and remove
sub-printable fragments left by the solid operations. If the fused Blender
mesh cannot be safely exported at STL precision, the CLI overlays the
original relief facets in XY and rebuilds the full and split solids from those
planes. It does not rasterize the relief or disable feature classes.
Sub-micron XY rounding and small adjustments at seams are possible. Each STL
is still checked for crossing surfaces before publication. Check bed fit,
relief and layer preview in your slicer: printer profiles, minimum feature size
and G-code are outside the CLI.

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
Seam repair uses [PyMeshFix 0.18.1](https://github.com/pyvista/pymeshfix/tree/v0.18.1)
under its GPLv3 terms. Manifold3D is pinned because newer versions can
change the volume and connectivity when reading dense binary STLs.
OSM2World and Blender retain their own licenses. The offline fixture is from
the pinned Touch Mapper revision.