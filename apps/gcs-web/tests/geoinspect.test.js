const test = require("node:test");
const assert = require("node:assert/strict");
const { geoReportHtml, geoUnavailableHtml, geoJpeg, geoRawImage, geoCountsText, geoSourceLine } = require("../geoinspect.js");

const s2 = {
  point: { lat: 12.8231, lon: 80.0442, name: "SRM Institute of Science and Technology, Kattankulathur", radius_m: 100 },
  imagery: {
    kind: "retrieved geographic imagery (not live drone footage)",
    provider: "Copernicus Sentinel-2 L2A (ESA), via Element 84 Earth Search", title: "S2B_44PLV_20260930_0_L2A",
    capture_date: "2026-09-30", gsd_m: 10.036, cloud_pct: 0, license: "Copernicus Sentinel data: free, full and open",
    attribution: "Contains modified Copernicus Sentinel data 2026", source_url: "https://sentinel-cogs.s3.us-west-2.amazonaws.com/x/TCI.tif",
    bounds: { south: 12.82, west: 80.04, north: 12.83, east: 80.05 }, width: 30, height: 30,
  },
  analysed_image_sha256: "abcdef0123456789abcdef", models_run: [], detections: [], counts: {},
  landcover: { method: "ESA Sentinel-2 Scene Classification (SCL) + NDVI", percent: { vegetation: 61.2, "not vegetated (built-up or bare soil)": 38.8 }, ndvi_mean: 0.41 },
  osm: { source: "OpenStreetMap ... NOT detected from the image", buildings: 23, roads_m: { service: 420, residential: 180 }, footpaths_m: 90,
    parking_areas: 2, single_trees_mapped: 0, green_areas: 3, water_features: 0, named_places: ["Tech Park <b>"] },
  limitations: ["This is retrieved geographic imagery ... It is not live footage from the simulated drone.", "no object detection was run"],
  attempts: [{ source: "OpenAerialMap", result: "ValueError: no OpenAerialMap imagery covers this point" }, { source: "Sentinel-2", result: "used" }],
  image_jpeg_b64: "SU1HQQ==", raw_image_b64: "UkFX", raw_image_mime: "image/png",
};

test("report says what it is, where it came from and when", () => {
  const h = geoReportHtml(s2);
  assert.match(h, /Retrieved geographic imagery - not live drone footage/);
  assert.match(h, /Copernicus Sentinel-2 L2A \(ESA\)/);
  assert.match(h, /<th>Captured<\/th><td>2026-09-30<\/td>/);
  assert.match(h, /10\.036 m per pixel \(30 × 30 px\)/);
  assert.match(h, /src="data:image\/jpeg;base64,SU1HQQ=="/);
  assert.match(h, /No object detection: the resolution is too coarse/);
  assert.match(h, /vegetation 61\.2%/);
  assert.match(h, /OpenStreetMap around it \(map data, not from the image\)/);
  assert.match(h, /23 building\(s\) · 600 m of road/);
  assert.match(h, /Tech Park &lt;b&gt;/);                 // escaped
  assert.match(h, /OpenAerialMap: ValueError: no OpenAerialMap imagery covers this point/);
  assert.match(h, /href="https:\/\/sentinel-cogs/);
});

test("detections are listed with model and confidence", () => {
  const h = geoReportHtml({ ...s2, models_run: ["YOLOv8n-OBB (aerial, DOTA classes)"], counts: { "small vehicle": 2 },
    detections: [{ model: "YOLOv8n-OBB (DOTA aerial)", class_name: "small vehicle", confidence: 0.81 }, { model: "YOLOv8n-OBB (DOTA aerial)", class_name: "small vehicle", confidence: 0.42 }] });
  assert.match(h, /2 × small vehicle/);
  assert.match(h, /small vehicle <span class="cbba-muted">81% · YOLOv8n-OBB/);
});

test("no image: plain error, nothing shown instead", () => {
  const h = geoUnavailableHtml({ message: "No openly licensed imagery could be retrieved for this point.", attempts: s2.attempts });
  assert.match(h, /No real image available for this point/);
  assert.match(h, /Nothing was substituted/);
  assert.doesNotMatch(h, /<img/);
});

test("only plain base64 becomes an image; helpers", () => {
  assert.equal(geoJpeg("SU1H"), "data:image/jpeg;base64,SU1H");
  assert.equal(geoJpeg('x" onerror="alert(1)'), null);
  assert.equal(geoJpeg(""), null);
  assert.equal(geoRawImage(s2), "data:image/png;base64,UkFX");
  assert.equal(geoRawImage({ raw_image_b64: "UkFX", raw_image_mime: "text/html" }), "data:image/jpeg;base64,UkFX");
  assert.equal(geoCountsText({ "small vehicle": 3, ship: 1 }), "3 × small vehicle, 1 × ship");
  assert.equal(geoSourceLine(s2), "Copernicus Sentinel-2 L2A (ESA), captured 2026-09-30, 10.036 m per pixel");
  assert.doesNotMatch(geoReportHtml({ ...s2, imagery: { ...s2.imagery, source_url: "javascript:alert(1)" } }), /href="javascript/);
});
