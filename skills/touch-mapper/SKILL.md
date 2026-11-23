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
