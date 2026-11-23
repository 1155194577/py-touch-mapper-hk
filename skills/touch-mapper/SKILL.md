---
name: touch-mapper
description: Generate local, printable tactile street maps from a latitude/longitude, bounding box, or OpenStreetMap XML. Use when asked for a tactile map for blind or low-vision readers, a 3D-printable map STL, or SVG/PDF artwork for an embosser.
---

# Touch Mapper

1. Get the location and output format. Use a local OSM 0.6 XML extract when
   provided. Otherwise use a confirmed latitude/longitude or bounding box; do
   not guess coordinates for an ambiguous place name. Fetching online sends
   the bounds to OpenStreetMap. Ask for a local extract if the area is private
   or the task must remain offline.
2. Confirm the print fits the printer bed or embossing sheet. If nothing is
   specified, use 17 × 17 cm at 1:3100 for a center point. For an embosser,
   allow an extra centimetre above the map in the SVG/PDF. For a crowded
   area, use `--content-mode no-buildings` or discuss `only-big-roads`
   before removing detail the reader needs.
3. Run `touch-mapper doctor` if the local runtime is not known to work.
   The CLI needs the installed Touch Mapper engine. If the command or
   `TOUCH_MAPPER_RUNTIME` is missing, follow the repository README's Docker
   instructions instead of installing a similarly named package.
4. Generate into a new directory. Treat stdout as a JSON manifest and stderr
   as diagnostics. Use `--verbose` for engine logs. Do not report success
   unless the command succeeds and the requested artifact exists. A successful
   STL export means the one-piece relief and base passed the solid geometry checks.
5. Return the requested STL, SVG or PDF, the final print dimensions, and
   attribution to [OpenStreetMap contributors](https://www.openstreetmap.org/copyright).
   Include `map-content.json` if the reader wants the feature description.
   Ask the recipient to verify bed fit and layer preview in their slicer; an STL
   is not sliced or printer-ready G-code.

For Hong Kong STL maps, use `--hk-tactile-paving` if the reader wants
Transport Department guide paths as raised dots. This can download a
DATA.GOV.HK file even with `--osm`; do not add it for offline work.
Check `hkTactilePaving.status` in the manifest before saying the layer is
present, and pass along its source and attribution when included.

## Site wayfinding: Chung Chi College by University MTR

- **Select the routes and landmarks a reader needs.** The fixed plan uses an
  irregular schematic outline: it keeps the station-facing approach, the loop
  around Lake Ad Excellentiam and Lingnan Stadium, and branches to campus
  buildings. It omits Exit B on the other side of the station and much of the
  surrounding street detail. It smooths routes into broad tactile bands and
  groups some adjoining buildings into one shape. Use a close `--bbox` (or
  matching offline extract), keep `--content-mode normal`, and choose a
  `--scale` that leaves useful separation and fits the bed. The CLI prints
  a rectangle from OSM, not a hand-edited schematic.
- **Keep routes and landmarks distinguishable by touch.** The plan contrasts
  pale route bands with raised yellow landmark shapes; the lake is a crescent
  and the running track a solid oval. The photo does not reliably distinguish
  raised from recessed paths. Set `--road-height-mm`, `--path-height-mm` or
  `--building-height-mm` to adjust their 3D relief. Mapped water gets a wave
  texture; the CLI does not reproduce the board's solid stadium oval or
  merge chosen footprints into landmark symbols.
- **Start at the viewer and provide a key.** The board faces the reader
  rather than putting north at the top; a north arrow and tactile north-line
  explain the orientation. It numbers selected features and route exits
  1–38, beginning with the bridge direction, architecture building and MTR
  exits D/C/A. The numbers identify features, not steps of a walking route,
  and link to a Chinese/English key with Braille. A red triangle marks “You
  Are Here”; burgundy dots mark bus stops, green dots mark other tactile-plan
  locations, and arrows name off-map destinations. Use `--marker LAT LON`
  for the verified installation point, not the nearest exit or the map
  center. The CLI gives a generic marker and a standard orientation cue; it
  has no viewer-alignment flag, dedicated exit/stop/plan symbols, destination
  arrows or numbered Braille key. Add those separately if requested and
  verify legibility by touch.

## Commands

```sh
# Offline: use bounds from OSM XML
touch-mapper generate --osm area.osm --scale 2100 --out-dir tactile-map

# Online: LAT LON; the 3D model marks the center unless --hide-marker is set
touch-mapper generate --center 22.2835 114.1585 --width-cm 12 --height-cm 10 \
  --scale 1800 --out-dir central-map

# Online: WEST SOUTH EAST NORTH (lon, lat, lon, lat)
touch-mapper generate --bbox 114.157 22.282 114.160 22.285 \
  --content-mode only-big-roads --scale 2400 --out-dir major-roads
```

If only the Docker image is available, mount the directory holding your OSM
file (and where the new output directory should appear):

```sh
docker run --rm --platform linux/amd64 --network none \
  --user "$(id -u):$(id -g)" -v "$PWD:/work" py-touch-mapper \
  generate --osm area.osm --scale 2100 --out-dir tactile-map
```

Use `map.stl` for a one-piece print, or import `map-ways.stl` and `map-rest.stl`
as parts of the same object in the slicer, keeping their shared origin. Skip an
empty ways STL. Use `map.svg` or `map.pdf` for embossing. If the
OpenStreetMap API rejects an area, shrink the bounds or use a local extract;
do not repeatedly retry the same oversized request.
