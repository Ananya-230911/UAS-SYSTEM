// Minimal GCS frontend -- see docs/adr/0005-frontend-stack.md for why this
// is plain HTML/JS instead of React for Phase 1.

const params = new URLSearchParams(window.location.search);
const API_BASE = params.get("api") || "http://localhost:8000";
const VEHICLE_ID = params.get("vehicle") || "sim-1";
const WS_BASE = API_BASE.replace(/^http/, "ws");

document.getElementById("vehicle-id").textContent = `(${VEHICLE_ID})`;

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
let marker = null;
let path = L.polyline([], { color: "#2563eb", weight: 2 }).addTo(map);

function setStatus(state, text) {
  const el = document.getElementById("connection-status");
  el.className = `status status--${state}`;
  el.textContent = text;
}

function fmt(value, digits = 1, suffix = "") {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${Number(value).toFixed(digits)}${suffix}`;
}

function renderTelemetry(sample) {
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
  document.getElementById("t-updated").textContent = new Date(sample.timestamp).toLocaleTimeString();

  if (sample.lat != null && sample.lon != null) {
    const latlng = [sample.lat, sample.lon];
    if (!marker) {
      marker = L.marker(latlng, { icon: droneIcon }).addTo(map);
      map.setView(latlng, map.getZoom());
    } else {
      marker.setLatLng(latlng);
    }
    path.addLatLng(latlng);
  }
}

function logCommand(text, isError = false) {
  const log = document.getElementById("command-log");
  const line = document.createElement("div");
  line.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
  if (isError) line.classList.add("error");
  log.prepend(line);
}

async function loadHistory() {
  try {
    const resp = await fetch(`${API_BASE}/vehicles/${VEHICLE_ID}/telemetry?limit=50`);
    if (!resp.ok) return;
    const samples = await resp.json();
    samples.forEach(renderTelemetry);
  } catch (err) {
    console.warn("failed to load telemetry history", err);
  }
}

function connectWebSocket() {
  const ws = new WebSocket(`${WS_BASE}/ws/telemetry/${VEHICLE_ID}`);
  ws.onopen = () => setStatus("connected", "live");
  ws.onclose = () => {
    setStatus("disconnected", "disconnected — retrying…");
    setTimeout(connectWebSocket, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "telemetry") renderTelemetry(msg);
  };
}

async function sendCommand(type, extra = {}) {
  try {
    const resp = await fetch(`${API_BASE}/vehicles/${VEHICLE_ID}/commands`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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

document.getElementById("btn-arm").addEventListener("click", () => sendCommand("ARM"));
document.getElementById("btn-disarm").addEventListener("click", () => sendCommand("DISARM"));
document
  .getElementById("btn-takeoff")
  .addEventListener("click", () => sendCommand("TAKEOFF", { altitude_m: 20 }));

setStatus("unknown", "connecting…");
loadHistory();
connectWebSocket();
