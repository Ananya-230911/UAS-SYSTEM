# ADR-0022: Real geographic imagery for inspection points

Status: accepted (October 2026)

## Context

The quick survey (ADR-0021) showed a webcam picture at each place. The
operator asked for the opposite: when a point is selected on the map, get
a **real image of that exact location** from its latitude/longitude,
analyse it with the existing YOLOv8 service, show the image, boxes,
labels, confidences, source and capture date -- and never use the
webcam, sample photos, stock or generated images for this, never
substitute an unrelated image, and keep the drone simulation working.

## Imagery sources compared

Checked October 2026. The deciding question was not only "free?" but
"may this project run its own AI on it?".

| Source | Resolution | Coverage | Capture date | Licence: display + our own AI | Used |
| --- | --- | --- | --- | --- | --- |
| **OpenAerialMap** (aerial / drone) | 0.02-1 m | patchy, only where uploaded | yes (`acquisition_start`) | CC-BY 4.0: yes, with credit | **1st** |
| **Copernicus Sentinel-2 L2A** via Element 84 Earth Search (STAC, no key), COGs on AWS | 10 m | whole world, every ~5 days | yes, per scene | Copernicus free, full and open: yes | **2nd** |
| Esri World Imagery | ~0.3-1 m | world | via Citations layer | display with an ArcGIS account; Esri: derived data "strictly for non-commercial use within ArcGIS", no direct automated extraction | no |
| Google Maps / Mapbox satellite | high | world | no | terms restrict deriving data | no |
| Landsat | 30 m | world | yes | open | no (too coarse) |
| NAIP (USA only) | 0.6 m | USA | yes | public domain | not yet (USA only) |
| EOX Sentinel-2 cloudless | 10 m | world, yearly mosaic | year only | CC BY-NC-SA (2016: CC BY) | no (Sentinel-2 L2A has real dates) |
| Commercial (Maxar, Airbus, Planet, ...) | 0.3-3 m | world | yes | paid licence | no (not free) |

**Conclusion:** no free source is both sharp enough for vehicles
everywhere *and* licensed for our own analysis. OpenAerialMap is when it
covers the point; otherwise Sentinel-2 is the honest worldwide answer at
10 m. For most places (including SRM, Chennai, at the time of writing --
to be confirmed on the operator's PC) that means Sentinel-2.

## Decision

`services/perception` (the existing YOLOv8 service) gets `app/geo/` and
`GET /geo/inspect?lat=&lon=&radius_m=`:

1. **Retrieve** (`imagery.py`): OpenAerialMap `/meta` for an image whose
   footprint contains the point (sharpest first, <= 1 m); else Earth
   Search for the newest Sentinel-2 L2A scene over the point with < 40 %
   cloud, choosing the first whose window is mostly clear by ESA's own
   Scene Classification band. Only a +/- radius window is read from the
   Cloud-Optimized GeoTIFFs (rasterio, HTTP range requests). The point
   must lie on valid data or the image is rejected. If nothing works: 404
   listing each source's answer -- **nothing is substituted**.
2. **Analyse** (`analysis.py`, `detection.py`), matched to resolution:
   - <= 1 m: **YOLOv8n-OBB** (Ultralytics, trained on DOTA aerial images:
     small/large vehicle, plane, ship, storage tank, swimming pool, courts,
     bridge, harbour, roundabout, helicopter). The existing COCO model is
     for ground-level photos; it is added only at <= 0.15 m (drone level),
     and person detections from above are flagged unreliable.
   - Sentinel-2 (10 m): **no object detection** (a car is smaller than a
     pixel); land cover from ESA's Scene Classification + NDVI from the
     real red/near-infrared bands.
   - RGB aerial: colour-based vegetation estimate, labelled as such.
3. **Map data** (`osm.py`): buildings, road lengths, footpaths, parking,
   green areas, trees, water and named places from OpenStreetMap
   (Overpass), labelled "map data, NOT detected from the image"; place
   name from Nominatim. Both used lightly, with a User-Agent, and cached.
4. **Record** for every result: provider, scene/title, capture date,
   ground resolution, cloud share, licence, attribution, source file URL,
   image bounds, the SHA-256 of the exact pixels YOLO analysed, which
   models ran, the per-resolution limitations, and what each source said.

Dashboard (`apps/gcs-web`):
- New **"📍 Inspect a real location"**: pick a point (or the map centre),
  see the retrieved image with crosshair, scale bar and boxes, the
  source/date table, detections with confidences, land cover, OSM data,
  limits -- and the image **laid over the map at its exact bounds** (raw
  pixels, PNG and pixelated when small, so 10 m pixels are shown as they
  are, not smoothed into fake detail).
- **Quick survey**: "At each place" defaults to the real image of that
  place (the webcam demo stays as an explicit option). Each place shows
  🛰 + objects found; the latest place's full report and overlay are
  shown. Labels everywhere: "Retrieved geographic imagery - not live
  drone footage"; the simulated drone (position, route) and the image
  are kept apart.
- `scripts/run_all.ps1` now starts the vision service by default
  (`-NoVision` to skip, `-Webcam` for the webcam demo).

Unchanged: CBBA, safety gate, Phase D/E, the flight simulator, the
webcam path (`/vehicles/{id}/perception`).

## Limits (also shown in the dashboard)

- Sentinel-2 cannot show vehicles, people or individual small buildings.
- OpenAerialMap coverage is patchy; where it exists, the date may be old.
- YOLOv8n-OBB knows 15 DOTA classes only; buildings, roads and trees are
  not object classes of any model here (they come from OSM, labelled).
- People: only at drone-level sharpness, and unreliable from above.
- Imagery can be months or years old; the date is always shown.

## Test results (sandbox, October 2026)

The sandbox cannot reach any imagery host (Earth Search, the AWS
Sentinel-2 bucket, OpenAerialMap, Overpass, Nominatim, Esri all blocked),
so **no live retrieval was possible here**; the first real run is on the
operator's PC. What was verified:

- `services/perception`: 49 passed. Georeferencing tests write real
  GeoTIFFs (UTM 44N like Sentinel-2; Web Mercator like OpenAerialMap) with
  a marker pixel at SRM's coordinates: the marker is the centre pixel of
  the retrieved window; Web Mercator pixel size is converted to ground
  metres; a point outside the image is refused; a cloudy window makes it
  try the next scene; no source -> ImageryUnavailable with each answer;
  YOLO receives exactly the retrieved pixels (SHA-256 matches the
  report); no YOLO on 10 m imagery; the real YOLOv8n-OBB weights load and
  report DOTA classes ("small vehicle" = class 10).
- `services/fleet-agent`: 560 passed, 1 skipped (survey uses geo mode;
  404 reported as "no real image", never replaced).
- `apps/gcs-web`: 32 passed (report renderer: labels, source/date table,
  escaping, no image on "unavailable").
- Browser end to end with the real services and a local stand-in for the
  catalogue APIs serving Sentinel-2-shaped test GeoTIFFs (clearly test
  data): pick SRM -> report with source/date/resolution, overlay exactly
  over the point, flight-control mission untouched; 3-drone survey ->
  12/12 places inspected with imagery, no page errors.
