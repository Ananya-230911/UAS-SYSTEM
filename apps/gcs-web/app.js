// Minimal GCS frontend -- see docs/adr/0005-frontend-stack.md for why this
// is plain HTML/JS instead of React for Phase 1/2/3.
//
// Phase 3 (docs/adr/0009-multi-vehicle-fleet.md): the page now tracks every
// vehicle it's told about (via GET /vehicles at load, and every message
// after that from the single /ws/fleet connection) and lets the user pick
// which one is "selected" -- selection drives the detail panel, the
// command/mission controls, and which vehicle's trail is drawn. Every
// other known vehicle still shows as a small marker on the map.

const params = new URLSearchParams(window.location.search);
const API_BASE = params.get("api") || "http://localhost:8000";
const WS_BASE = API_BASE.replace(/^http/, "ws");
// Phase 5 (docs/adr/0011-api-authentication.md): opt-in shared API key.
// Empty when the API isn't configured with one (Phase 1-4 default), in
// which case every header/query-param below is simply absent/blank and
// the API's own no-op check (require_api_key/require_ws_api_key) lets
// everything through unchanged.
const API_KEY = params.get("api_key") || "";

function authHeaders(extra = {}) {
  return API_KEY ? { ...extra, "X-API-Key": API_KEY } : extra;
}

function withApiKey(url) {
  if (!API_KEY) return url;
  const u = new URL(url);
  u.searchParams.set("api_key", API_KEY);
  return u.toString();
}

let selectedVehicleId = params.get("vehicle") || null;
const vehicles = {}; // vehicle_id -> latest telemetry/vehicle sample seen
const fleetMarkers = {}; // vehicle_id -> L.marker, one per known vehicle

const map = L.map("map").setView([37.4275, -122.1697], 17);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "© OpenStreetMap contributors",
  maxZoom: 19,
}).addTo(map);

const droneIcon = L.divIcon({
  className: "drone-marker",
  html: "▲",
  iconSize: [20, 20],
});
let path = L.polyline([], { color: "#2563eb", weight: 2 }).addTo(map);

// Phase 2 mission-planning state: waypoints staged locally until uploaded.
// Scoped to whichever vehicle is currently selected -- see selectVehicle().
let draftWaypoints = []; // [{lat, lon, alt_m}]
let draftMarkers = []; // parallel array of L.marker
let draftPath = L.polyline([], { color: "#f59e0b", weight: 2, dashArray: "6 6" }).addTo(map);
let currentMissionId = null;

// Phase 6a (docs/adr/0012-geofencing-failsafe.md) geofence-drawing state.
// Reuses the exact "click the map to add points" interaction Phase 2
// already established for missions -- draftFencePoints is only staged
// locally until Save, same lifecycle as draftWaypoints. fenceDrawMode
// decides which one a map click feeds.
let fenceDrawMode = false;
let draftFencePoints = []; // [{lat, lon}]
let draftFenceMarkers = [];
let draftFencePolygon = L.polygon([], {
  color: "#dc2626",
  weight: 2,
  dashArray: "4 4",
  fillOpacity: 0.05,
}).addTo(map);
// The vehicle's currently-saved fence, loaded on selectVehicle().
let savedFencePolygon = L.polygon([], { color: "#dc2626", weight: 2, fillOpacity: 0.08 }).addTo(
  map
);

map.on("click", (e) => {
  if (fenceDrawMode) {
    draftFencePoints.push({ lat: e.latlng.lat, lon: e.latlng.lng });
    renderDraftFence();
  } else {
    draftWaypoints.push({ lat: e.latlng.lat, lon: e.latlng.lng, alt_m: 20 });
    renderDraftWaypoints();
  }
});

function renderDraftWaypoints() {
  draftMarkers.forEach((m) => map.removeLayer(m));
  draftMarkers = draftWaypoints.map((wp, i) =>
    L.marker([wp.lat, wp.lon], {
      icon: L.divIcon({ className: "waypoint-marker", iconSize: [12, 12] }),
      title: `Waypoint ${i + 1}`,
    }).addTo(map)
  );
  draftPath.setLatLngs(draftWaypoints.map((wp) => [wp.lat, wp.lon]));

  const list = document.getElementById("waypoint-list");
  list.innerHTML = "";
  draftWaypoints.forEach((wp, i) => {
    const row = document.createElement("div");
    row.innerHTML = `<span>#${i + 1}: ${wp.lat.toFixed(5)}, ${wp.lon.toFixed(5)} @ ${wp.alt_m}m</span>`;
    const removeBtn = document.createElement("button");
    removeBtn.textContent = "✕";
    removeBtn.addEventListener("click", () => {
      draftWaypoints.splice(i, 1);
      renderDraftWaypoints();
    });
    row.appendChild(removeBtn);
    list.appendChild(row);
  });
}

function setMissionStatus(text) {
  document.getElementById("mission-status").textContent = text;
}

function setFenceStatus(text) {
  document.getElementById("fence-status").textContent = text;
}

function renderDraftFence() {
  draftFenceMarkers.forEach((m) => map.removeLayer(m));
  draftFenceMarkers = draftFencePoints.map((p, i) =>
    L.marker([p.lat, p.lon], {
      icon: L.divIcon({ className: "fence-point-marker", iconSize: [10, 10] }),
      title: `Fence point ${i + 1}`,
    }).addTo(map)
  );
  draftFencePolygon.setLatLngs(draftFencePoints.map((p) => [p.lat, p.lon]));
}

function clearFenceDraft() {
  draftFencePoints = [];
  renderDraftFence();
}

function toggleFenceDrawMode() {
  fenceDrawMode = !fenceDrawMode;
  document.getElementById("btn-fence-draw").textContent = fenceDrawMode
    ? "Drawing… (click Draw to stop)"
    : "Draw";
}

async function loadFence(vehicleId) {
  savedFencePolygon.setLatLngs([]);
  if (!vehicleId) return;
  try {
    const resp = await fetch(`${API_BASE}/vehicles/${vehicleId}/geofence`, {
      headers: authHeaders(),
    });
    if (resp.status === 404) {
      setFenceStatus("No geofence set.");
      return;
    }
    if (!resp.ok) return;
    const fence = await resp.json();
    savedFencePolygon.setLatLngs(fence.points.map((p) => [p.lat, p.lon]));
    setFenceStatus(`Active fence: ${fence.points.length} points.`);
  } catch (err) {
    console.warn("failed to load geofence", err);
  }
}

async function saveFence() {
  if (!selectedVehicleId) {
    setFenceStatus("Select a vehicle first.");
    return;
  }
  if (draftFencePoints.length < 3) {
    setFenceStatus("Add at least 3 points by clicking the map first.");
    return;
  }
  try {
    const resp = await fetch(`${API_BASE}/vehicles/${selectedVehicleId}/geofence`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ points: draftFencePoints }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      setFenceStatus(`Save FAILED: ${body.detail || "unknown error"}`);
      return;
    }
    savedFencePolygon.setLatLngs(body.points.map((p) => [p.lat, p.lon]));
    clearFenceDraft();
    setFenceStatus(`Active fence: ${body.points.length} points.`);
  } catch (err) {
    setFenceStatus(`Save FAILED: ${err}`);
  }
}

async function deleteFence() {
  if (!selectedVehicleId) {
    setFenceStatus("Select a vehicle first.");
    return;
  }
  try {
    const resp = await fetch(`${API_BASE}/vehicles/${selectedVehicleId}/geofence`, {
      method: "DELETE",
      headers: authHeaders(),
    });
    if (!resp.ok && resp.status !== 404) {
      const body = await resp.json();
      setFenceStatus(`Delete FAILED: ${body.detail || "unknown error"}`);
      return;
    }
    savedFencePolygon.setLatLngs([]);
    setFenceStatus("No geofence set.");
  } catch (err) {
    setFenceStatus(`Delete FAILED: ${err}`);
  }
}

function setStatus(state, text) {
  const el = document.getElementById("connection-status");
  el.className = `status status--${state}`;
  el.textContent = text;
}

function fmt(value, digits = 1, suffix = "") {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${Number(value).toFixed(digits)}${suffix}`;
}

// --- Fleet: every known vehicle gets a marker; the selected one is drawn
// as the highlighted drone icon, everyone else as a small dot (green if
// armed, gray otherwise). ---

function iconFor(vehicleId, sample) {
  if (vehicleId === selectedVehicleId) return droneIcon;
  const armed = sample && sample.armed;
  return L.divIcon({ className: `fleet-marker${armed ? " armed" : ""}`, iconSize: [14, 14] });
}

function updateFleetMarker(vehicleId, sample) {
  if (sample.lat == null || sample.lon == null) return;
  const latlng = [sample.lat, sample.lon];
  if (!fleetMarkers[vehicleId]) {
    fleetMarkers[vehicleId] = L.marker(latlng, { icon: iconFor(vehicleId, sample) })
      .addTo(map)
      .on("click", (e) => {
        L.DomEvent.stopPropagation(e); // don't also register as a waypoint click
        selectVehicle(vehicleId);
      });
  } else {
    fleetMarkers[vehicleId].setLatLng(latlng);
    fleetMarkers[vehicleId].setIcon(iconFor(vehicleId, sample));
  }
}

function renderFleetList() {
  const container = document.getElementById("fleet-list");
  const ids = Object.keys(vehicles).sort();
  if (ids.length === 0) {
    container.innerHTML = '<p class="hint">Waiting for vehicles…</p>';
    return;
  }
  container.innerHTML = "";
  ids.forEach((id) => {
    const sample = vehicles[id];
    const row = document.createElement("div");
    row.className = `fleet-row${id === selectedVehicleId ? " selected" : ""}`;
    row.innerHTML =
      `<span class="fleet-row-id">${id}</span>` +
      `<span class="fleet-row-meta">${sample.armed ? "ARMED" : "disarmed"} · ` +
      `${sample.flight_mode || "—"} · ${fmt(sample.battery_pct, 0, "%")}</span>`;
    row.addEventListener("click", () => selectVehicle(id));
    container.appendChild(row);
  });
}

function selectVehicle(vehicleId) {
  if (!vehicleId || vehicleId === selectedVehicleId) return;
  selectedVehicleId = vehicleId;
  document.getElementById("vehicle-id").textContent = `(${vehicleId})`;
  path.setLatLngs([]);
  clearMission();
  clearFenceDraft();
  loadFence(vehicleId);

  // Re-render every marker's icon so the previously-selected vehicle drops
  // back to a plain fleet dot and the newly-selected one gets the drone icon.
  Object.keys(fleetMarkers).forEach((id) => {
    fleetMarkers[id].setIcon(iconFor(id, vehicles[id]));
  });

  const known = vehicles[vehicleId];
  if (known && known.lat != null && known.lon != null) {
    map.setView([known.lat, known.lon], map.getZoom());
  }

  renderFleetList();
  loadHistory();
}

function renderTelemetry(sample) {
  if (sample.vehicle_id !== selectedVehicleId) return;
  document.getElementById("t-armed").textContent = sample.armed ? "ARMED" : "disarmed";
  document.getElementById("t-mode").textContent = sample.flight_mode || "—";
  document.getElementById("t-alt").textContent = fmt(sample.relative_alt_m, 1, " m");
  document.getElementById("t-speed").textContent = fmt(sample.groundspeed_ms, 1, " m/s");
  document.getElementById("t-heading").textContent = fmt(sample.heading_deg, 0, "°");
  document.getElementById("t-battery").textContent = fmt(sample.battery_pct, 0, " %");
  document.getElementById("t-gps").textContent =
    sample.gps_fix_type != null
      ? `fix ${sample.gps_fix_type} (${sample.satellites_visible ?? "?"} sats)`
      : "—";
  document.getElementById("t-pos").textContent =
    sample.lat != null && sample.lon != null
      ? `${sample.lat.toFixed(5)}, ${sample.lon.toFixed(5)}`
      : "—";
  document.getElementById("t-waypoint").textContent =
    sample.current_waypoint_seq != null ? `#${sample.current_waypoint_seq + 1}` : "—";
  document.getElementById("t-updated").textContent = new Date(sample.timestamp).toLocaleTimeString();
  const emergencyEl = document.getElementById("t-emergency");
  emergencyEl.textContent =
    sample.emergency_state === "GEOFENCE_BREACH" ? "BREACHED — auto-RTL issued" : "OK";
  emergencyEl.classList.toggle("emergency", sample.emergency_state === "GEOFENCE_BREACH");

  if (sample.lat != null && sample.lon != null) {
    path.addLatLng([sample.lat, sample.lon]);
  }
}

function logCommand(text, isError = false) {
  const log = document.getElementById("command-log");
  const line = document.createElement("div");
  line.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
  if (isError) line.classList.add("error");
  log.prepend(line);
}

async function discoverVehicles() {
  try {
    const resp = await fetch(`${API_BASE}/vehicles`, { headers: authHeaders() });
    if (!resp.ok) return;
    const list = await resp.json();
    list.forEach((v) => {
      // GET /vehicles doesn't carry position -- that arrives via telemetry
      // (history load or the fleet WS). Seed what we have so the fleet
      // list shows a previously-seen vehicle even before new data arrives.
      vehicles[v.vehicle_id] = { ...vehicles[v.vehicle_id], ...v };
    });
    renderFleetList();
    if (!selectedVehicleId && list.length > 0) {
      selectVehicle(list[0].vehicle_id);
    } else if (selectedVehicleId) {
      loadHistory();
      loadFence(selectedVehicleId); // selectVehicle() wasn't called for a URL-preselected vehicle
    }
  } catch (err) {
    console.warn("failed to discover vehicles", err);
  }
}

async function loadHistory() {
  if (!selectedVehicleId) return;
  try {
    const resp = await fetch(`${API_BASE}/vehicles/${selectedVehicleId}/telemetry?limit=50`, {
      headers: authHeaders(),
    });
    if (!resp.ok) return;
    const samples = await resp.json();
    samples.forEach((s) => {
      // Merge, don't replace: a TelemetrySample doesn't carry
      // emergency_state or the other vehicle-level fields GET /vehicles
      // already seeded (see discoverVehicles()) -- overwriting outright
      // would silently forget them.
      vehicles[s.vehicle_id] = { ...vehicles[s.vehicle_id], ...s };
      updateFleetMarker(s.vehicle_id, s);
      renderTelemetry(s);
    });
    renderFleetList();
  } catch (err) {
    console.warn("failed to load telemetry history", err);
  }
}

function connectFleetWebSocket() {
  const ws = new WebSocket(withApiKey(`${WS_BASE}/ws/fleet`));
  ws.onopen = () => setStatus("connected", "live");
  ws.onclose = () => {
    setStatus("disconnected", "disconnected — retrying…");
    setTimeout(connectFleetWebSocket, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type !== "telemetry") return;
    vehicles[msg.vehicle_id] = msg;
    updateFleetMarker(msg.vehicle_id, msg);
    if (!selectedVehicleId) selectVehicle(msg.vehicle_id); // first vehicle seen becomes the default focus
    renderFleetList();
    renderTelemetry(msg);
  };
}

async function sendCommand(type, extra = {}) {
  if (!selectedVehicleId) {
    logCommand(`${type} FAILED: no vehicle selected`, true);
    return;
  }
  try {
    const resp = await fetch(`${API_BASE}/vehicles/${selectedVehicleId}/commands`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ type, ...extra }),
    });
    const body = await resp.json();
    if (!resp.ok || body.status === "FAILED") {
      logCommand(`${type} FAILED: ${body.error || body.detail || "unknown error"}`, true);
    } else {
      logCommand(`${type} → ${body.status}`);
    }
  } catch (err) {
    logCommand(`${type} FAILED: ${err}`, true);
  }
}

async function uploadMission() {
  if (!selectedVehicleId) {
    setMissionStatus("Select a vehicle first.");
    return;
  }
  if (draftWaypoints.length === 0) {
    setMissionStatus("Add at least one waypoint by clicking the map first.");
    return;
  }
  try {
    const resp = await fetch(`${API_BASE}/vehicles/${selectedVehicleId}/missions`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ waypoints: draftWaypoints }),
    });
    const body = await resp.json();
    if (!resp.ok || body.status !== "UPLOADED") {
      setMissionStatus(`Upload FAILED: ${body.error || body.detail || "unknown error"}`);
      return;
    }
    currentMissionId = body.mission_id;
    setMissionStatus(`Mission ${currentMissionId.slice(0, 8)} uploaded (${draftWaypoints.length} waypoints). Ready to start.`);
  } catch (err) {
    setMissionStatus(`Upload FAILED: ${err}`);
  }
}

async function startMission() {
  if (!selectedVehicleId) {
    setMissionStatus("Select a vehicle first.");
    return;
  }
  if (!currentMissionId) {
    setMissionStatus("Upload a mission before starting it.");
    return;
  }
  try {
    const resp = await fetch(
      `${API_BASE}/vehicles/${selectedVehicleId}/missions/${currentMissionId}/start`,
      { method: "POST", headers: authHeaders() }
    );
    const body = await resp.json();
    if (!resp.ok || body.status !== "ACTIVE") {
      setMissionStatus(`Start FAILED: ${body.error || body.detail || "unknown error"}`);
      return;
    }
    setMissionStatus(`Mission ${currentMissionId.slice(0, 8)} ACTIVE.`);
  } catch (err) {
    setMissionStatus(`Start FAILED: ${err}`);
  }
}

function clearMission() {
  draftWaypoints = [];
  currentMissionId = null;
  renderDraftWaypoints();
  setMissionStatus("No mission uploaded.");
}

document.getElementById("btn-arm").addEventListener("click", () => sendCommand("ARM"));
document.getElementById("btn-disarm").addEventListener("click", () => sendCommand("DISARM"));
document
  .getElementById("btn-takeoff")
  .addEventListener("click", () => sendCommand("TAKEOFF", { altitude_m: 20 }));
document.getElementById("btn-rtl").addEventListener("click", () => sendCommand("RTL"));

document.getElementById("btn-mission-clear").addEventListener("click", clearMission);
document.getElementById("btn-mission-upload").addEventListener("click", uploadMission);
document.getElementById("btn-mission-start").addEventListener("click", startMission);

document.getElementById("btn-fence-draw").addEventListener("click", toggleFenceDrawMode);
document.getElementById("btn-fence-clear").addEventListener("click", clearFenceDraft);
document.getElementById("btn-fence-save").addEventListener("click", saveFence);
document.getElementById("btn-fence-delete").addEventListener("click", deleteFence);

setStatus("unknown", "connecting…");
if (selectedVehicleId) {
  document.getElementById("vehicle-id").textContent = `(${selectedVehicleId})`;
}
discoverVehicles();
connectFleetWebSocket();
