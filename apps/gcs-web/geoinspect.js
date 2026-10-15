// Real geographic inspection (docs/adr/0022-geographic-imagery.md).
//
// Pick a point on the map -> services/perception GET /geo/inspect -> a
// REAL image of that place from an openly licensed source (OpenAerialMap,
// else Copernicus Sentinel-2), what YOLOv8 / land-cover analysis found in
// it, and OpenStreetMap map data around it. Shown as retrieved imagery
// with its source and capture date -- never as live drone footage.
//
// geoReportHtml() is pure (Node-testable, tests/geoinspect.test.js) and is
// also used by the quick survey (cbba.js) for each inspected place.

function geoEscape(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Only plain base64 JPEG from our own service becomes an <img>.
function geoJpeg(b64) {
  return typeof b64 === "string" && b64.length > 0 && /^[A-Za-z0-9+/=]+$/.test(b64) ? `data:image/jpeg;base64,${b64}` : null;
}

// The raw analysed pixels (PNG when small, JPEG when large) for the map overlay.
function geoRawImage(rep) {
  const mime = rep && rep.raw_image_mime === "image/png" ? "image/png" : "image/jpeg";
  const b64 = rep && rep.raw_image_b64;
  return typeof b64 === "string" && b64.length > 0 && /^[A-Za-z0-9+/=]+$/.test(b64) ? `data:${mime};base64,${b64}` : null;
}

function geoCountsText(counts) {
  const e = Object.entries(counts || {}).filter(([, k]) => k > 0).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return e.length ? e.map(([n, k]) => `${k} × ${n}`).join(", ") : "";
}

// One-line summary of where a picture came from.
function geoSourceLine(rep) {
  const im = rep.imagery || {};
  return `${(im.provider || "").split(",")[0]}${im.capture_date ? `, captured ${im.capture_date}` : ", capture date unknown"}` +
    `${im.gsd_m != null ? `, ${im.gsd_m} m per pixel` : ""}`;
}

function geoReportHtml(rep, opts = {}) {
  const esc = geoEscape;
  const im = rep.imagery || {};
  const p = rep.point || {};
  const img = geoJpeg(rep.image_jpeg_b64);
  const rows = [
    ["Source", im.provider],
    ["Image", im.title],
    ["Captured", im.capture_date || "not given by the source"],
    ["Resolution", im.gsd_m != null ? `${im.gsd_m} m per pixel (${im.width} × ${im.height} px)` : ""],
    ["Cloud in view", im.cloud_pct != null ? `${im.cloud_pct}%` : ""],
    ["Licence", im.license],
    ["Credit", im.attribution],
  ].filter(([, v]) => v !== "" && v != null);
  const link = /^https:\/\//.test(im.source_url || "") ? `<a href="${esc(im.source_url)}" target="_blank" rel="noopener noreferrer">source file</a>` : esc(im.source_url || "");

  const parts = [];
  parts.push('<div class="geo-kind">🛰 Retrieved geographic imagery - not live drone footage</div>');
  if (!opts.compact) {
    parts.push(`<p class="cbba-line"><b>${esc(p.name || "Selected point")}</b><br><span class="cbba-muted">${Number(p.lat).toFixed(5)}, ${Number(p.lon).toFixed(5)} · ±${esc(p.radius_m)} m</span></p>`);
  }
  if (img) parts.push(`<img class="geo-img" src="${img}" alt="retrieved image of the selected point, with detections">`);
  parts.push(`<table class="geo-table">${rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}<tr><th>File</th><td>${link}</td></tr></table>`);

  // What the image itself shows
  parts.push('<div class="perception-heading">Found in the image</div>');
  if (rep.models_run && rep.models_run.length) {
    const dets = (rep.detections || []).slice().sort((a, b) => b.confidence - a.confidence);
    parts.push(`<p class="cbba-line">Models: ${esc(rep.models_run.join(" + "))}</p>`);
    parts.push(dets.length
      ? `<p class="cbba-line"><b>${esc(geoCountsText(rep.counts))}</b></p><ul class="cbba-list geo-dets">${dets.slice(0, 15).map((d) => `<li>${esc(d.class_name)} <span class="cbba-muted">${(d.confidence * 100).toFixed(0)}% · ${esc(d.model)}</span></li>`).join("")}${dets.length > 15 ? `<li class="cbba-muted">+ ${dets.length - 15} more</li>` : ""}</ul>`
      : '<p class="cbba-line">No objects of the models\' classes were found.</p>');
  } else {
    parts.push('<p class="cbba-line">No object detection: the resolution is too coarse (see limits below).</p>');
  }
  const lc = rep.landcover;
  if (lc && lc.percent) {
    parts.push(`<p class="cbba-line"><b>Land cover:</b> ${Object.entries(lc.percent).map(([k, v]) => `${esc(k)} ${v}%`).join(" · ")}` +
      `${lc.ndvi_mean != null ? ` · NDVI ${lc.ndvi_mean}` : ""}<br><span class="cbba-muted">${esc(lc.method)}</span>` +
      `${lc.ndvi_warning ? `<br><span class="cbba-issues">${esc(lc.ndvi_warning)}</span>` : ""}</p>`);
  }

  // Map data, kept separate
  const o = rep.osm || {};
  parts.push('<div class="perception-heading">OpenStreetMap around it (map data, not from the image)</div>');
  if (o.error) {
    parts.push(`<p class="cbba-line cbba-muted">${esc(o.error)}</p>`);
  } else if (o.source) {
    const roads = Object.entries(o.roads_m || {});
    const roadTotal = roads.reduce((a, [, v]) => a + v, 0);
    parts.push(`<p class="cbba-line">${o.buildings} building(s) · ${roadTotal} m of road${roads.length ? ` (${roads.slice(0, 3).map(([k, v]) => `${esc(k)} ${v} m`).join(", ")})` : ""}` +
      ` · ${o.footpaths_m} m of paths · ${o.parking_areas} parking area(s) · ${o.green_areas} green area(s) · ${o.single_trees_mapped} mapped tree(s) · ${o.water_features} water feature(s)` +
      `${(o.named_places || []).length ? `<br>Named: ${o.named_places.map(esc).join(", ")}` : ""}</p>`);
  }

  // Honesty
  parts.push('<div class="perception-heading">Limits of this result</div>');
  parts.push(`<ul class="cbba-list geo-limits">${(rep.limitations || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`);
  parts.push(`<p class="cbba-line cbba-muted geo-proof">Exact pixels analysed: SHA-256 ${esc(String(rep.analysed_image_sha256 || "").slice(0, 16))}… · ` +
    `Sources tried: ${(rep.attempts || []).map((a) => `${esc(a.source)}: ${esc(a.result)}`).join("; ")}</p>`);
  return parts.join("");
}

// The service answered 404: no source had imagery. Say so plainly.
function geoUnavailableHtml(detail) {
  const d = detail && typeof detail === "object" ? detail : { message: String(detail || "No imagery available.") };
  return `<p class="cbba-line cbba-issues"><b>No real image available for this point.</b> ${geoEscape(d.message || "")}</p>` +
    `<ul class="cbba-list">${(d.attempts || []).map((a) => `<li>${geoEscape(a.source)}: ${geoEscape(a.result)}</li>`).join("")}</ul>` +
    '<p class="cbba-line cbba-muted">Nothing was substituted.</p>';
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { geoReportHtml, geoUnavailableHtml, geoJpeg, geoRawImage, geoCountsText, geoSourceLine, geoEscape };
}

// ---------------------------------------------------------------------------
// Browser: the "Inspect a real location" tool (uses app.js's `map` and
// PERCEPTION_BASE).
if (typeof document !== "undefined" && typeof L !== "undefined" && document.getElementById("geo-output")) (function () {
  const $ = (id) => document.getElementById(id);
  const layer = L.layerGroup().addTo(map);
  let picking = false;

  function setPicking(on) {
    picking = on;
    window.cbbaPicking = on; // app.js: don't add a flight-control waypoint
    map.getContainer().style.cursor = on ? "crosshair" : "";
    $("btn-geo-pick").classList.toggle("cbba-active", on);
    $("btn-geo-pick").textContent = on ? "Click the map… (Esc to cancel)" : "Pick a point on the map";
  }

  // Show the retrieved image on the map, exactly over the area it covers.
  function overlay(rep) {
    layer.clearLayers();
    const b = rep.imagery && rep.imagery.bounds;
    const raw = geoRawImage(rep);
    if (b && raw && $("geo-overlay").checked) {
      L.imageOverlay(raw, [[b.south, b.west], [b.north, b.east]], { opacity: 0.9, interactive: false, className: "geo-overlay" }).addTo(layer);
      L.rectangle([[b.south, b.west], [b.north, b.east]], { color: "#f97316", weight: 2, fill: false, interactive: false }).addTo(layer);
    }
    L.circleMarker([rep.point.lat, rep.point.lon], { radius: 6, color: "#fff", weight: 2, fillColor: "#f97316", fillOpacity: 1 })
      .bindTooltip("Inspection point").addTo(layer);
  }

  async function inspect(lat, lon) {
    const out = $("geo-output");
    const radius = Number($("geo-radius").value) || 100;
    layer.clearLayers();
    L.circleMarker([lat, lon], { radius: 6, color: "#fff", weight: 2, fillColor: "#f97316", fillOpacity: 1 }).addTo(layer);
    out.innerHTML = `<p class="cbba-line">Retrieving real imagery for ${lat.toFixed(5)}, ${lon.toFixed(5)}… (finding a scene and reading only this small area can take 10-40 s)</p>`;
    let resp, body;
    try {
      resp = await fetch(`${PERCEPTION_BASE}/geo/inspect?lat=${lat}&lon=${lon}&radius_m=${radius}`);
      body = await resp.json();
    } catch (err) {
      out.innerHTML = `<p class="cbba-line cbba-issues">Could not reach the camera / vision service at ${geoEscape(PERCEPTION_BASE)} (${geoEscape(err)}). ` +
        "Start it: <code>powershell -ExecutionPolicy Bypass -File scripts\\run_perception.ps1</code></p>";
      return;
    }
    if (resp.status === 404 && body && body.detail) {
      out.innerHTML = geoUnavailableHtml(body.detail);
      return;
    }
    if (!resp.ok) {
      out.innerHTML = `<p class="cbba-line cbba-issues">The vision service answered ${resp.status}: ${geoEscape(JSON.stringify(body.detail || body))}</p>`;
      return;
    }
    window.geoLastReport = body;
    out.innerHTML = geoReportHtml(body);
    overlay(body);
  }

  map.on("click", (e) => {
    if (!picking) return;
    setPicking(false);
    inspect(e.latlng.lat, e.latlng.lng);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && picking) setPicking(false);
  });
  $("btn-geo-pick").addEventListener("click", () => setPicking(!picking));
  $("btn-geo-centre").addEventListener("click", () => {
    const c = map.getCenter();
    inspect(c.lat, c.lng);
  });
  $("geo-overlay").addEventListener("change", () => window.geoLastReport && overlay(window.geoLastReport));
  $("btn-geo-clear").addEventListener("click", () => {
    layer.clearLayers();
    $("geo-output").innerHTML = "";
  });
})();
