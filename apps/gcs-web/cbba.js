// Multi-drone CBBA simulation panel (docs/CBBA_PLAN.md, docs/adr/0018).
//
// 1. Mission setup: the operator places drones and inspection points on
//    the map (anywhere in the world), sets batteries, sensors and radio,
//    and optionally draws the fence.
// 2. Plan: services/fleet-agent's read-only server (app/server.py, port
//    8020) runs CBBA -- every drone is an agent that bids and talks to the
//    drones it can hear -- then the deterministic safety gate.
// 3. How the drones decided: a round-by-round replay of the agents'
//    bids, messages and give-ups, from the server's trace.
// 4. Simulated flight: the drones move along their planned routes in the
//    browser; a drone can be made to fail mid-flight, and CBBA re-plans
//    the places it hadn't reached.
//
// Everything is a simulation. Nothing is sent to any vehicle, and no LLM is
// involved anywhere in planning or safety.
//
// The first half is pure logic (no `document`/`L`), require()-able from
// Node for tests (tests/cbba.test.js), like freshness.js. The second half
// only runs in the browser, after app.js (it uses app.js's `map` and
// `params` globals).

const CBBA_COLORS = ["#2563eb", "#d97706", "#059669", "#7c3aed", "#db2777", "#0891b2", "#65a30d", "#b45309"];
const CBBA_SENSORS = ["camera", "thermal", "lidar"];
const CBBA_EARTH_R = 6371000;

function cbbaDroneColor(droneId, allDroneIds) {
  const i = [...allDroneIds].sort().indexOf(droneId);
  return i < 0 ? "#94a3b8" : CBBA_COLORS[i % CBBA_COLORS.length];
}

// Same flat-earth distance as the server (app/safety/geofence.py).
function cbbaDistanceM(a, b) {
  const rad = Math.PI / 180;
  const north = (b.lat - a.lat) * rad * CBBA_EARTH_R;
  const east = (b.lon - a.lon) * rad * CBBA_EARTH_R * Math.cos(((a.lat + b.lat) / 2) * rad);
  return Math.hypot(north, east);
}

// A rectangle around every drone and point, padded by padM metres.
function cbbaAutoFence(drones, points, padM = 120) {
  const all = [...drones, ...points];
  if (all.length === 0) return null;
  const lats = all.map((x) => x.lat);
  const lons = all.map((x) => x.lon);
  const midLat = (Math.min(...lats) + Math.max(...lats)) / 2;
  const dLat = padM / ((CBBA_EARTH_R * Math.PI) / 180);
  const dLon = dLat / Math.cos((midLat * Math.PI) / 180);
  const s = Math.min(...lats) - dLat, n = Math.max(...lats) + dLat;
  const w = Math.min(...lons) - dLon, e = Math.max(...lons) + dLon;
  return [{ lat: s, lon: w }, { lat: s, lon: e }, { lat: n, lon: e }, { lat: n, lon: w }];
}

// Radio links between drones: everyone, or only pairs within rangeM.
// A drone whose radio is broken has no links at all.
function cbbaLinks(drones, mode, rangeM) {
  const links = [];
  for (let i = 0; i < drones.length; i++) {
    for (let j = i + 1; j < drones.length; j++) {
      const a = drones[i], b = drones[j];
      if (a.radioBroken || b.radioBroken) continue;
      if (mode === "range" && cbbaDistanceM(a, b) > rangeM) continue;
      links.push([a.id, b.id].sort());
    }
  }
  return links;
}

// Editor state -> the server's Scenario JSON (services/fleet-agent/app/cbba/models.py).
function cbbaBuildScenario(state) {
  const fence = state.fence && state.fence.length >= 3 ? state.fence : cbbaAutoFence(state.drones, state.points);
  const n = state.drones.length;
  const links = cbbaLinks(state.drones, state.radioMode, state.rangeM);
  const comms = links.length === (n * (n - 1)) / 2 ? {} : { links };
  return {
    drones: state.drones.map((d) => {
      const out = {
        drone_id: d.id,
        start: { lat: d.lat, lon: d.lon, alt_m: 0 },
        battery_pct: Number(d.battery),
        capabilities: [...d.caps],
        health: d.offline ? "OFFLINE" : "OK",
      };
      if (d.home) out.home = { lat: d.home.lat, lon: d.home.lon, alt_m: 0 };
      return out;
    }),
    points: state.points.map((p) => ({
      point_id: p.id,
      lat: p.lat,
      lon: p.lon,
      required_capability: p.sensor || null,
      value: Number(p.value || 100),
      deadline_s: p.deadline ? Number(p.deadline) : null,
      inspect_duration_s: Number(p.hover || 0),
    })),
    geofence: { points: (fence || []).map((f) => ({ lat: f.lat, lon: f.lon })) },
    comms,
  };
}

// The server's checked scenario (after an instruction) -> editor state.
// Only the fields an instruction may change are copied: a place's sensor,
// priority (value) and deadline, whether it is still in the mission, and
// whether a drone is offline. Positions, fence and radio stay as they are.
function cbbaApplyScenario(state, sc) {
  const pts = Object.fromEntries(sc.points.map((p) => [p.point_id, p]));
  const drs = Object.fromEntries(sc.drones.map((d) => [d.drone_id, d]));
  return {
    ...state,
    points: state.points
      .filter((p) => pts[p.id])
      .map((p) => ({
        ...p,
        sensor: pts[p.id].required_capability || "",
        value: pts[p.id].value,
        deadline: pts[p.id].deadline_s || "",
      })),
    drones: state.drones.map((d) => (drs[d.id] ? { ...d, offline: drs[d.id].health === "OFFLINE" } : d)),
  };
}

// Problems to fix before asking the server (plain English).
function cbbaCheckState(state) {
  const problems = [];
  if (state.drones.length === 0) problems.push("Add at least one drone (+ Drone, then click the map).");
  if (state.points.length === 0) problems.push("Add at least one place to inspect (+ Place, then click the map).");
  const names = [...state.drones.map((d) => d.id), ...state.points.map((p) => p.id)];
  if (names.some((x) => !String(x).trim())) problems.push("Every drone and place needs a name.");
  const dup = names.filter((x, i) => names.indexOf(x) !== i);
  if (dup.length) problems.push(`Names must be different: ${[...new Set(dup)].join(", ")}.`);
  for (const d of state.drones) {
    const b = Number(d.battery);
    if (!(b >= 0 && b <= 100)) problems.push(`${d.id}: battery must be 0-100%.`);
  }
  for (const p of state.points) {
    if (p.deadline && !(Number(p.deadline) > 0)) problems.push(`${p.id}: deadline must be a positive number of seconds.`);
  }
  if (state.fence && state.fence.length > 0 && state.fence.length < 3) problems.push("A drawn fence needs at least 3 corners.");
  return problems;
}

// point_id -> { state, owners, reason } for a final result:
//   "assigned" (one drone), "double" (2+ drones -- blocked),
//   "unassigned" (no drone could take it), "ignored" (outside the fence).
function cbbaPointStatuses(scenario, result) {
  const owners = {};
  for (const plan of result.plans) {
    for (const pid of plan.path) (owners[pid] = owners[pid] || []).push(plan.drone_id);
  }
  const reasons = {};
  for (const x of result.unassigned) reasons[x.item_id] = x.reason;
  for (const x of result.excluded_points) reasons[x.item_id] = x.reason;
  const ignored = new Set(result.excluded_points.map((x) => x.item_id));
  const out = {};
  for (const p of scenario.points) {
    const o = owners[p.point_id] || [];
    let state = "assigned";
    if (ignored.has(p.point_id)) state = "ignored";
    else if (o.length === 0) state = "unassigned";
    else if (o.length > 1) state = "double";
    out[p.point_id] = { state, owners: o, reason: reasons[p.point_id] || "" };
  }
  return out;
}

// { drone: [points] } -> { point: [drones] }
function cbbaClaimOwners(claims) {
  const out = {};
  for (const [drone, pts] of Object.entries(claims)) {
    for (const p of pts) (out[p] = out[p] || []).push(drone);
  }
  return out;
}

// The server's trace -> replay steps, two per round:
//   "bid":  each drone alone picks places and bids (no talking yet);
//   "talk": they exchange messages; higher bids win, losers drop places.
function cbbaReplaySteps(trace) {
  const steps = [];
  for (const r of trace) {
    const bids = r.events.filter((e) => e.kind === "bid");
    const drops = r.events.filter((e) => e.kind === "dropped");
    const conflicts = Object.entries(cbbaClaimOwners(r.wants)).filter(([, ds]) => ds.length > 1).length;
    steps.push({
      round: r.round,
      phase: "bid",
      claims: r.wants,
      links: [],
      events: bids,
      text:
        `Round ${r.round}, step 1 - thinking alone: each drone looks at the places, its battery, its ` +
        `sensors and the bids it has heard so far, and claims the places it can do best. ` +
        (bids.length ? `${bids.length} new bid(s).` : "No new bids.") +
        (conflicts ? ` ${conflicts} place(s) are wanted by more than one drone (red).` : ""),
    });
    steps.push({
      round: r.round,
      phase: "talk",
      claims: r.claims,
      links: r.links,
      events: drops,
      text:
        `Round ${r.round}, step 2 - talking: each drone sends its bids to every drone it can hear ` +
        `(${r.messages} message(s) over ${r.links.length} radio link(s)). Each place goes to the highest bid. ` +
        (drops.length ? `${drops.length} place(s) given up.` : "No conflicts - nobody had to give anything up."),
    });
  }
  return steps;
}

// One event -> one plain-English line.
function cbbaEventText(e) {
  if (e.kind === "bid") return `${e.drone} bids ${e.bid} for ${e.point}`;
  if (e.reason === "outbid") {
    return `${e.drone} gives up ${e.point}: ${e.winner} bid more (${e.winner_bid} vs ${e.bid})`;
  }
  if (e.reason === "after_lost") {
    return `${e.drone} also drops ${e.point}: it was planned after a place it lost, so that bid no longer holds`;
  }
  return `${e.drone} drops ${e.point} for now (conflicting news) and will bid again`;
}

// Live-agent simulation playback (server: POST /sim/run). Frames are one
// per tick, in time order; the frame shown at time t is the last one not
// after t.
function cbbaFrameAt(frames, t) {
  if (!frames.length) return null;
  let lo = 0, hi = frames.length - 1;
  if (t <= frames[0].t) return frames[0];
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (frames[mid].t <= t) lo = mid;
    else hi = mid - 1;
  }
  return frames[lo];
}

// point_id -> drone that first inspected it, from the event log up to t.
function cbbaInspectorsUpTo(events, t) {
  const out = {};
  for (const e of events) {
    if (e.t > t) break;
    if (e.kind !== "inspect") continue;
    const m = /inspected (\S+?)(?: \(|\.$)/.exec(e.text);
    if (m && !(m[1] in out)) out[m[1]] = e.drone;
  }
  return out;
}

// The plan in force at time t: the last planning round started by then.
function cbbaEpochAt(epochs, t) {
  let cur = null;
  for (const ep of epochs) if (ep.started_t <= t) cur = ep;
  return cur;
}

const CBBA_STATUS_TEXT = {
  planning: "deciding", flying: "flying", holding: "holding (re-planning)", returning: "returning home",
  home: "home", lost: "LOST", offline: "offline", "lost-link": "no radio contact",
};

function cbbaGroupIssues(issues) {
  const groups = [];
  const byCode = {};
  for (const i of issues) {
    if (!byCode[i.code]) {
      byCode[i.code] = { code: i.code, count: 0, messages: [] };
      groups.push(byCode[i.code]);
    }
    byCode[i.code].count += 1;
    byCode[i.code].messages.push(i.message);
  }
  return groups;
}

function cbbaAgreementText(result) {
  if (!result.converged) return `The drones did NOT reach agreement within ${result.rounds} rounds.`;
  const groups = result.comms_groups || [];
  if (groups.length > 1) {
    const list = groups.map((g) => "{" + g.join(", ") + "}").join(" and ");
    return `The radio network is split: ${list} can't hear each other. Each group agreed on its own ` +
      `(round ${result.last_change_round}, ${result.messages_sent} messages), so the groups may claim the same points.`;
  }
  return `The drones agreed in round ${result.last_change_round} using ${result.messages_sent} messages.`;
}

function cbbaEscape(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// "P04-solar-panels" -> "P04"
function cbbaShortName(pointId) {
  return String(pointId).split("-")[0];
}

// ---- quick survey (Phase F) ---------------------------------------------------

// {car: 2, person: 4} -> "4 people, 2 cars" (largest first).
function cbbaCountsText(counts) {
  const entries = Object.entries(counts || {}).filter(([, k]) => k > 0);
  if (!entries.length) return "nothing detected";
  const plural = { person: "people", bus: "buses" };
  return entries
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([name, k]) => `${k} ${k === 1 ? name : plural[name] || name + "s"}`)
    .join(", ");
}

function cbbaSumCounts(list) {
  const out = {};
  for (const c of list) for (const [k, v] of Object.entries(c || {})) out[k] = (out[k] || 0) + v;
  return out;
}

// Bid messages delivered in the last windowS seconds up to t, as
// unordered drone pairs: [["D1", "D2"], ...]. Frames carry them as
// (sender, receiver) in frame.talk.
function cbbaTalkAt(frames, t, windowS = 2) {
  const seen = new Set();
  const out = [];
  for (const f of frames) {
    if (f.t > t) break;
    if (f.t <= t - windowS || !f.talk) continue;
    for (const [a, b] of f.talk) {
      const pair = [a, b].sort();
      const key = pair.join("|");
      if (!seen.has(key)) {
        seen.add(key);
        out.push(pair);
      }
    }
  }
  return out;
}

// First inspection of each point, in time order: [{point, drone, t}].
function cbbaInspections(events) {
  const out = [];
  const seen = new Set();
  for (const e of events) {
    if (e.kind !== "inspect") continue;
    const m = /inspected (\S+?)(?: \(|\.$)/.exec(e.text);
    if (m && !seen.has(m[1])) {
      seen.add(m[1]);
      out.push({ point: m[1], drone: e.drone, t: e.t });
    }
  }
  return out;
}

// Leaflet bounds -> the server's MapView, kept inside valid lat/lon.
function cbbaMapView(south, west, north, east) {
  const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
  return { south: clamp(south, -89.9, 89.9), west: clamp(west, -180, 180), north: clamp(north, -89.9, 89.9), east: clamp(east, -180, 180) };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    cbbaDroneColor, cbbaDistanceM, cbbaAutoFence, cbbaLinks, cbbaBuildScenario, cbbaCheckState,
    cbbaPointStatuses, cbbaClaimOwners, cbbaApplyScenario, cbbaReplaySteps, cbbaEventText,
    cbbaFrameAt, cbbaInspectorsUpTo, cbbaEpochAt, CBBA_STATUS_TEXT,
    cbbaGroupIssues, cbbaAgreementText, cbbaEscape, cbbaShortName, CBBA_COLORS,
    cbbaCountsText, cbbaSumCounts, cbbaTalkAt, cbbaInspections, cbbaMapView,
  };
}

// ---------------------------------------------------------------------------
// Browser only below.

if (typeof document !== "undefined" && typeof L === "undefined") {
  const out = document.getElementById("cbba-output");
  if (out) {
    out.textContent =
      "The map library (Leaflet) did not load, so nothing can be drawn. " +
      "Pull the latest code (Leaflet is bundled in apps/gcs-web/vendor/) and reload with Ctrl+Shift+R.";
  }
}

// Wrapped in a function so none of these names can clash with app.js's
// globals (it already has e.g. `droneIcon`).
if (typeof document !== "undefined" && typeof L !== "undefined") (function () {
  const CBBA_BASE = params.get("cbba") || "http://localhost:8020";
  const STORE_KEY = "uas-cbba-mission-v1";
  const layer = L.layerGroup().addTo(map);
  const camLayer = L.layerGroup().addTo(map); // camera results (redrawn only when they change)
  const $ = (id) => document.getElementById(id);
  const esc = cbbaEscape;

  // ---- state -------------------------------------------------------------
  let state = { drones: [], points: [], fence: null, radioMode: "all", rangeM: 300, missionId: null };
  let proposal = null; // last /cbba/instruct answer
  let clickMode = null; // "drone" | "point" | "fence"
  let fenceDraft = [];
  let view = "edit"; // "edit" | "plan" | "replay" | "fly"
  let last = null; // last server response {scenario, result, reference, trace}
  let steps = [];
  let stepIdx = 0;
  // Live-agent simulation playback
  const sim = { data: null, t: 0, running: false, speed: 10, raf: null, lastTs: null, failures: [] };
  // Quick survey: is the mission one made by "Go", and what each camera saw
  // ("point|drone" -> "pending" or the server's CameraResult).
  const survey = { active: false, camera: {}, shownKey: "" };

  function save() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(state));
    } catch (e) {
      /* private mode etc. -- the panel still works, it just won't remember */
    }
  }
  function load() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (raw) state = { ...state, ...JSON.parse(raw) };
      return !!raw;
    } catch (e) {
      return false;
    }
  }

  const ids = () => state.drones.map((d) => d.id);
  const color = (id) => cbbaDroneColor(id, ids());

  function nextName(prefix, existing) {
    let i = 1;
    while (existing.includes(`${prefix}${i}`)) i++;
    return `${prefix}${i}`;
  }

  // Any edit makes the old plan stale.
  function edited() {
    stopSim();
    sim.data = null;
    clearCamera();
    last = null;
    steps = [];
    view = "edit";
    save();
    renderAll();
    $("cbba-output").textContent = "Mission changed - click Run CBBA to plan it.";
  }

  // ---- map clicks ----------------------------------------------------------
  function setClickMode(mode) {
    clickMode = clickMode === mode ? null : mode;
    window.cbbaPicking = !!clickMode;
    if (clickMode !== "fence" && fenceDraft.length) fenceDraft = [];
    const hints = {
      drone: "Click the map to place a drone. Click as many times as you like. Press Esc or the button again to stop.",
      point: "Click the map to place a place to inspect. Press Esc or the button again to stop.",
      fence: "Click the map to add fence corners (3 or more), then press Finish fence.",
    };
    $("cbba-click-hint").textContent = clickMode ? hints[clickMode] : "";
    for (const [m, id] of [["drone", "btn-cbba-add-drone"], ["point", "btn-cbba-add-point"], ["fence", "btn-cbba-draw-fence"]]) {
      $(id).classList.toggle("cbba-active", clickMode === m);
    }
    $("btn-cbba-draw-fence").textContent = clickMode === "fence" ? "Finish fence" : "Draw fence";
    map.getContainer().style.cursor = clickMode ? "crosshair" : "";
    renderMap();
  }

  map.on("click", (e) => {
    if (!clickMode) return;
    const { lat, lng: lon } = e.latlng;
    if (clickMode === "drone") {
      state.drones.push({ id: nextName("D", ids()), lat, lon, battery: 90, caps: ["camera"], offline: false, radioBroken: false });
    } else if (clickMode === "point") {
      state.points.push({ id: nextName("P", state.points.map((p) => p.id)), lat, lon, sensor: "", deadline: "", hover: 0, value: 100 });
    } else if (clickMode === "fence") {
      fenceDraft.push({ lat, lon });
      renderMap();
      return;
    }
    edited();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && clickMode) {
      if (clickMode === "fence") fenceDraft = [];
      setClickMode(clickMode);
    }
  });

  function finishFence() {
    if (fenceDraft.length >= 3) {
      state.fence = fenceDraft;
      fenceDraft = [];
      setClickMode("fence");
      edited();
    } else {
      fenceDraft = [];
      setClickMode("fence");
    }
  }

  // ---- map drawing -----------------------------------------------------------
  function droneIcon(id, opts = {}) {
    const c = opts.grey ? "#94a3b8" : color(id);
    const cls = "cbba-drone" + (opts.grey ? " cbba-drone--off" : "") + (opts.failed ? " cbba-drone--failed" : "") + (opts.nolink ? " cbba-drone--nolink" : "");
    return L.divIcon({
      className: "cbba-drone-wrap",
      html: `<div class="${cls}" style="color:${c}">▲<span>${esc(id)}</span></div>`,
      iconSize: [24, 24],
      iconAnchor: [12, 12],
    });
  }
  function pointIcon(p, fill, extraCls = "") {
    const sensorCls = p.sensor ? " cbba-point--sensor" : "";
    return L.divIcon({
      className: "cbba-point-wrap",
      html: `<div class="cbba-point${sensorCls}${extraCls}" style="background:${fill}">${esc(cbbaShortName(p.id))}</div>`,
      iconSize: [34, 20],
      iconAnchor: [17, 10],
    });
  }

  function currentFence() {
    return state.fence && state.fence.length >= 3 ? state.fence : cbbaAutoFence(state.drones, state.points);
  }

  function renderMap() {
    layer.clearLayers();
    const noBubble = { bubblingMouseEvents: false };
    const editable = view === "edit" || view === "plan";

    const fence = currentFence();
    if (fence) {
      L.polygon(fence.map((f) => [f.lat, f.lon]), {
        color: "#0f172a", weight: 2, dashArray: state.fence ? "6 5" : "2 6", fill: false, interactive: false,
      }).addTo(layer);
    }
    if (fenceDraft.length) {
      L.polyline(fenceDraft.map((f) => [f.lat, f.lon]), { color: "#7c3aed", weight: 2, dashArray: "4 4", interactive: false }).addTo(layer);
      for (const f of fenceDraft) L.circleMarker([f.lat, f.lon], { radius: 4, color: "#7c3aed", interactive: false }).addTo(layer);
    }

    const result = last && last.result;
    const pointsById = {};
    if (last) for (const p of last.scenario.points) pointsById[p.point_id] = p;

    // Routes: the plan (plan view) or the plan in force at time t (sim view)
    const simFrame = view === "sim" && sim.data ? cbbaFrameAt(sim.data.frames, sim.t) : null;
    const simEpoch = view === "sim" && sim.data ? cbbaEpochAt(sim.data.epochs, sim.t) : null;
    const routePlans = view === "plan" && result ? result.plans : simEpoch && simEpoch.result.dispatch_allowed ? simEpoch.result.plans : [];
    for (const plan of routePlans) {
      if (!plan.route.length) continue;
      L.polyline(plan.route.map((w) => [w.lat, w.lon]), {
        color: color(plan.drone_id), weight: view === "sim" ? 3 : 4, opacity: view === "sim" ? 0.35 : 0.85, ...noBubble,
      }).bindTooltip(`${plan.drone_id}: ${Math.round(plan.route_m)} m at ${plan.altitude_m} m altitude`).addTo(layer);
    }
    // Radio links in use at time t
    if (simFrame) {
      const at = Object.fromEntries(simFrame.drones.map((d) => [d.id, d]));
      for (const [a, b] of simFrame.links) {
        if (!at[a] || !at[b]) continue;
        L.polyline([[at[a].lat, at[a].lon], [at[b].lat, at[b].lon]], { color: "#7c3aed", weight: 1.5, dashArray: "2 5", opacity: 0.8, interactive: false }).addTo(layer);
      }
    }

    // Bids being exchanged right now (the drones deciding who does what)
    if (simFrame) {
      const at = Object.fromEntries(simFrame.drones.map((d) => [d.id, d]));
      for (const [a, b] of cbbaTalkAt(sim.data.frames, sim.t)) {
        if (!at[a] || !at[b]) continue;
        L.polyline([[at[a].lat, at[a].lon], [at[b].lat, at[b].lon]], { color: "#f97316", weight: 5, opacity: 0.85, ...noBubble })
          .bindTooltip(`${esc(a)} ⇄ ${esc(b)}: sending each other their bids (who wants which place)`).addTo(layer);
      }
    }

    // Radio links (replay "talk" step)
    const step = view === "replay" ? steps[stepIdx] : null;
    if (step && step.links.length) {
      const pos = Object.fromEntries(state.drones.map((d) => [d.id, d]));
      for (const [a, b] of step.links) {
        if (!pos[a] || !pos[b]) continue;
        L.polyline([[pos[a].lat, pos[a].lon], [pos[b].lat, pos[b].lon]], {
          color: "#7c3aed", weight: 2, dashArray: "3 6", opacity: 0.9, interactive: false,
        }).addTo(layer);
      }
    }

    // Points
    let statuses = null, owners = null;
    if (result && view === "plan") statuses = cbbaPointStatuses(last.scenario, result);
    const inspectors = view === "sim" && sim.data ? cbbaInspectorsUpTo(sim.data.events, sim.t) : null;
    const plannedOwner = {};
    if (simEpoch) for (const pl of simEpoch.result.plans) for (const pid of pl.path) plannedOwner[pid] = pl.drone_id;
    if (step) owners = cbbaClaimOwners(step.claims);
    state.points.forEach((p, i) => {
      let fill = "#ffffff", extra = "", tip = `<b>${esc(p.id)}</b>${p.sensor ? ` (needs ${esc(p.sensor)})` : ""}`;
      if (p.deadline) tip += `<br>Deadline: ${esc(p.deadline)} s`;
      if (statuses && statuses[p.id]) {
        const st = statuses[p.id];
        if (st.state === "assigned") {
          fill = color(st.owners[0]);
          tip += `<br>Inspected by ${esc(st.owners[0])}`;
        } else if (st.state === "double") {
          fill = "#dc2626";
          tip += `<br>Claimed by ${esc(st.owners.join(" and "))} - blocked`;
        } else {
          fill = st.state === "ignored" ? "#94a3b8" : "#ffffff";
          tip += `<br>${esc(st.reason)}`;
        }
      } else if (owners) {
        const o = owners[p.id] || [];
        fill = o.length === 0 ? "#ffffff" : o.length > 1 ? "#dc2626" : color(o[0]);
        tip += o.length ? `<br>Wanted by ${esc(o.join(", "))}` : "<br>Nobody has claimed it yet";
      }
      if (inspectors) {
        if (inspectors[p.id]) {
          fill = color(inspectors[p.id]);
          extra = " cbba-point--done";
          tip += `<br>✓ inspected by ${esc(inspectors[p.id])}`;
        } else if (plannedOwner[p.id]) {
          fill = "#ffffff";
          extra = " cbba-point--planned";
          tip += `<br>to be inspected by ${esc(plannedOwner[p.id])}`;
        }
      }
      const m = L.marker([p.lat, p.lon], { icon: pointIcon(p, fill, extra), draggable: editable, ...noBubble }).bindTooltip(tip);
      m.on("dragend", (e) => {
        const ll = e.target.getLatLng();
        state.points[i].lat = ll.lat;
        state.points[i].lon = ll.lng;
        edited();
      });
      m.addTo(layer);
    });

    // Drones
    state.drones.forEach((d, i) => {
      let at = { lat: d.lat, lon: d.lon };
      let failed = false;
      let tip = `${esc(d.id)}${d.offline ? " (OFFLINE)" : ""}: ${esc(d.caps.join(" + ") || "no sensors")}, ` +
        `battery ${Math.round(d.battery)}%${d.radioBroken ? ", radio broken" : ""}`;
      const fd = simFrame && simFrame.drones.find((x) => x.id === d.id);
      if (fd) {
        at = { lat: fd.lat, lon: fd.lon };
        failed = fd.status === "lost";
        tip = `${esc(d.id)}: ${esc(CBBA_STATUS_TEXT[fd.status] || fd.status)}, battery ${Math.round(fd.battery)}%, planning round ${fd.epoch}`;
      }
      const grey = d.offline || failed || (fd && fd.status === "offline");
      const m = L.marker([at.lat, at.lon], { icon: droneIcon(d.id, { grey, failed, nolink: fd && fd.status === "lost-link" }), draggable: editable, ...noBubble }).bindTooltip(tip);
      m.on("dragend", (e) => {
        const ll = e.target.getLatLng();
        state.drones[i].lat = ll.lat;
        state.drones[i].lon = ll.lng;
        edited();
      });
      m.addTo(layer);
    });
  }

  function fitAll() {
    const fence = currentFence();
    if (fence) map.fitBounds(L.latLngBounds(fence.map((f) => [f.lat, f.lon])).pad(0.1));
  }

  // ---- setup lists -------------------------------------------------------------
  function renderSetup() {
    const dl = $("cbba-drone-list");
    if (!state.drones.length) {
      dl.innerHTML = '<p class="cbba-empty">No drones yet.</p>';
    } else {
      dl.innerHTML = state.drones
        .map((d, i) => {
          const caps = CBBA_SENSORS.map(
            (s) => `<label class="cbba-chip"><input type="checkbox" data-d="${i}" data-f="cap" value="${s}" ${d.caps.includes(s) ? "checked" : ""}/>${s}</label>`
          ).join("");
          return `<div class="cbba-row">
            <div class="cbba-row-line">
              <span class="cbba-dot" style="background:${color(d.id)}"></span>
              <input class="cbba-name" data-d="${i}" data-f="id" value="${esc(d.id)}" aria-label="drone name"/>
              <label class="cbba-mini">battery <input type="number" min="0" max="100" data-d="${i}" data-f="battery" value="${esc(d.battery)}"/>%</label>
              <button class="cbba-x" data-d="${i}" data-f="remove" title="Remove">×</button>
            </div>
            <div class="cbba-row-line cbba-row-sub">${caps}
              <label class="cbba-chip cbba-chip--warn"><input type="checkbox" data-d="${i}" data-f="offline" ${d.offline ? "checked" : ""}/>offline</label>
              <label class="cbba-chip cbba-chip--warn"><input type="checkbox" data-d="${i}" data-f="radioBroken" ${d.radioBroken ? "checked" : ""}/>radio broken</label>
            </div></div>`;
        })
        .join("");
    }

    const pl = $("cbba-point-list");
    if (!state.points.length) {
      pl.innerHTML = '<p class="cbba-empty">No places yet.</p>';
    } else {
      pl.innerHTML = state.points
        .map((p, i) => {
          const opts = ["", ...CBBA_SENSORS]
            .map((s) => `<option value="${s}" ${p.sensor === s ? "selected" : ""}>${s ? "needs " + s : "any drone"}</option>`)
            .join("");
          const v = Number(p.value || 100);
          const prios = [[50, "low priority"], [100, "normal priority"], [150, "high priority"]];
          if (!prios.some(([x]) => x === v)) prios.push([v, `priority ${v}`]);
          const prioOpts = prios.map(([x, label]) => `<option value="${x}" ${x === v ? "selected" : ""}>${label}</option>`).join("");
          return `<div class="cbba-row"><div class="cbba-row-line">
              <input class="cbba-name" data-p="${i}" data-f="id" value="${esc(p.id)}" aria-label="place name"/>
              <select data-p="${i}" data-f="sensor" aria-label="sensor needed">${opts}</select>
              <button class="cbba-x" data-p="${i}" data-f="remove" title="Remove">×</button>
            </div><div class="cbba-row-line cbba-row-sub">
              <select data-p="${i}" data-f="value" aria-label="priority">${prioOpts}</select>
              <input class="cbba-deadline" type="number" min="1" placeholder="no deadline (s)" title="Must be reached within this many seconds of take-off" data-p="${i}" data-f="deadline" value="${esc(p.deadline || "")}"/>
            </div></div>`;
        })
        .join("");
    }

    $("cbba-radio-mode").value = state.radioMode;
    $("cbba-range").value = state.rangeM;
    $("cbba-range-wrap").hidden = state.radioMode !== "range";
    $("cbba-fence-status").textContent = state.fence
      ? `Fence: drawn by you (${state.fence.length} corners).`
      : "Fence: automatic (a box around all drones and places).";
    $("btn-cbba-auto-fence").hidden = !state.fence;
  }

  function onListChange(e) {
    const t = e.target;
    const f = t.dataset.f;
    if (!f) return;
    if (t.dataset.d !== undefined) {
      const d = state.drones[Number(t.dataset.d)];
      if (f === "cap") d.caps = t.checked ? [...new Set([...d.caps, t.value])] : d.caps.filter((c) => c !== t.value);
      else if (f === "offline" || f === "radioBroken") d[f] = t.checked;
      else if (f === "battery") d.battery = t.value;
      else if (f === "id") d.id = t.value.trim();
    } else if (t.dataset.p !== undefined) {
      const p = state.points[Number(t.dataset.p)];
      if (f === "sensor") p.sensor = t.value;
      else if (f === "value") p.value = Number(t.value);
      else if (f === "deadline") p.deadline = t.value;
      else if (f === "id") p.id = t.value.trim();
    }
    edited();
  }
  function onListClick(e) {
    const t = e.target;
    if (t.dataset.f !== "remove") return;
    if (t.dataset.d !== undefined) state.drones.splice(Number(t.dataset.d), 1);
    if (t.dataset.p !== undefined) state.points.splice(Number(t.dataset.p), 1);
    edited();
  }

  // ---- plan --------------------------------------------------------------------
  function serverDown(err) {
    return (
      `Could not reach the CBBA server on ${CBBA_BASE} (${err}). Start it in a new terminal:\n` +
      "cd services\\fleet-agent\n$env:PYTHONPATH = \".\"\n" +
      "..\\..\\.venv\\Scripts\\python.exe -m uvicorn app.server:app --port 8020"
    );
  }

  async function postPlan(scenario, meta = {}) {
    const resp = await fetch(`${CBBA_BASE}/missions/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario, mission_id: state.missionId, source: "operator", ...meta }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail || resp.status;
      throw new Error(`the server rejected the mission: ${detail}`);
    }
    if (body.mission) {
      state.missionId = body.mission.mission_id;
      save();
    }
    return body;
  }

  async function runPlan(meta = {}) {
    if (clickMode) setClickMode(clickMode);
    stopSim();
    const problems = cbbaCheckState(state);
    if (problems.length) {
      $("cbba-output").innerHTML = '<ul class="cbba-list cbba-issues">' + problems.map((p) => `<li>${esc(p)}</li>`).join("") + "</ul>";
      return;
    }
    $("cbba-output").textContent = "The drones are planning…";
    try {
      last = await postPlan(cbbaBuildScenario(state), meta);
    } catch (err) {
      $("cbba-output").textContent = String(err).includes("rejected") ? String(err) : serverDown(err);
      return;
    }
    steps = cbbaReplaySteps(last.trace || []);
    stepIdx = 0;
    view = "plan";
    renderAll();
    fitAll();
  }

  function renderResult() {
    const out = $("cbba-output");
    if (!last) return;
    const { scenario, result, reference } = last;
    const caps = Object.fromEntries(scenario.drones.map((d) => [d.drone_id, d.capabilities.join("+") || "no sensors"]));
    const parts = [];
    parts.push(
      result.dispatch_allowed
        ? '<div class="cbba-verdict cbba-verdict--ok">Safety gate: PASSED</div>'
        : '<div class="cbba-verdict cbba-verdict--blocked">Safety gate: BLOCKED</div>'
    );
    parts.push(`<div class="cbba-label">${esc(result.label)} &middot; simulation only, nothing is flown</div>`);
    if (last.mission) {
      const m = last.mission;
      parts.push(`<div class="cbba-label">Saved as mission ${esc(m.mission_id)}, version ${m.version}${m.source !== "operator" ? ` (${esc(m.source)})` : ""}</div>`);
    }
    parts.push(`<p class="cbba-line">${esc(cbbaAgreementText(result))}</p>`);
    parts.push('<div class="perception-heading">Who inspects what</div><ul class="cbba-list">');
    for (const plan of result.plans) {
      const dot = `<span class="cbba-dot" style="background:${color(plan.drone_id)}"></span>`;
      const route = plan.path.length ? plan.path.map(cbbaShortName).map(esc).join(" → ") : "nothing";
      parts.push(`<li>${dot}<b>${esc(plan.drone_id)}</b> <span class="cbba-muted">(${esc(caps[plan.drone_id])}, ${plan.altitude_m} m)</span><br>${route}</li>`);
    }
    for (const x of result.excluded_drones) {
      parts.push(`<li><span class="cbba-dot" style="background:#94a3b8"></span><b>${esc(x.item_id)}</b> <span class="cbba-muted">left out: ${esc(x.reason)}</span></li>`);
    }
    parts.push("</ul>");
    const notDone = [...result.unassigned, ...result.excluded_points];
    if (notDone.length) {
      parts.push('<div class="perception-heading">Not inspected</div><ul class="cbba-list">');
      for (const x of notDone) parts.push(`<li><b>${esc(cbbaShortName(x.item_id))}</b> <span class="cbba-muted">${esc(x.reason)}</span></li>`);
      parts.push("</ul>");
    }
    if (result.safety.issues.length) {
      parts.push('<div class="perception-heading">Why it is blocked</div><ul class="cbba-list cbba-issues">');
      for (const g of cbbaGroupIssues(result.safety.issues)) {
        const more = g.count > 1 ? ` <span class="cbba-muted">(and ${g.count - 1} more like this)</span>` : "";
        parts.push(`<li><b>${esc(g.code)}</b>: ${esc(g.messages[0])}${more}</li>`);
      }
      parts.push("</ul>");
    } else {
      parts.push(`<p class="cbba-line cbba-muted">Central reference planner (SGA): ${reference.same_allocation ? "same answer" : "different answer"}.</p>`);
    }
    out.innerHTML = parts.join("");
  }

  // ---- replay -----------------------------------------------------------------
  function renderReplay() {
    const box = $("cbba-replay");
    const has = steps.length > 0;
    $("btn-cbba-step-prev").disabled = !has || (view === "replay" && stepIdx === 0);
    $("btn-cbba-step-next").disabled = !has || (view === "replay" && stepIdx === steps.length - 1);
    $("btn-cbba-step-show").disabled = !has;
    if (!has) {
      box.innerHTML = '<p class="cbba-empty">Run CBBA first, then step through the drones\' conversation here.</p>';
      return;
    }
    if (view !== "replay") {
      box.innerHTML = `<p class="cbba-empty">${steps.length / 2} round(s) recorded. Press "Start replay" to watch them decide step by step.</p>`;
      return;
    }
    const s = steps[stepIdx];
    const isLast = stepIdx === steps.length - 1;
    const evs = s.events.length
      ? '<ul class="cbba-list">' + s.events.map((e) => `<li><span class="cbba-dot" style="background:${color(e.drone)}"></span>${esc(cbbaEventText(e))}</li>`).join("") + "</ul>"
      : "";
    box.innerHTML =
      `<div class="cbba-step-count">Step ${stepIdx + 1} of ${steps.length}</div>` +
      `<p class="cbba-line">${esc(s.text)}</p>${evs}` +
      (isLast ? `<p class="cbba-line"><b>Nothing changes any more - this is the final plan.</b> Press "Back to plan" to see the routes.</p>` : "");
  }

  function startReplay() {
    if (!steps.length) return;
    stopSim();
    if (view !== "replay") {
      view = "replay";
      stepIdx = 0;
    } else {
      view = "plan";
    }
    renderAll();
  }
  function stepBy(n) {
    if (!steps.length) return;
    if (view !== "replay") {
      view = "replay";
      stepIdx = 0;
    } else {
      stepIdx = Math.max(0, Math.min(steps.length - 1, stepIdx + n));
    }
    renderAll();
  }

  // ---- live agents (simulated flight) -------------------------------------------
  const FAILURE_TEXT = { drone_lost: "is lost (crash)", radio_cut: "radio breaks", radio_restored: "radio works again", battery_drop: "battery drops to" };

  function simConfig() {
    return {
      message_loss: Number($("cbba-sim-loss").value),
      message_delay_ticks: Number($("cbba-sim-delay").value),
      heartbeat_timeout_s: Math.max(2, Number($("cbba-sim-timeout").value) || 5),
    };
  }

  async function runSim(keepTime = false) {
    if (clickMode) setClickMode(clickMode);
    const problems = cbbaCheckState(state);
    if (problems.length) {
      $("cbba-sim-status").textContent = problems.join(" ");
      return;
    }
    stopSim();
    const t = keepTime ? sim.t : 0;
    if (!keepTime) clearCamera();
    $("cbba-sim-status").textContent = "The drones are flying the mission (simulation running on the server)…";
    try {
      const resp = await fetch(`${CBBA_BASE}/sim/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario: cbbaBuildScenario(state), config: simConfig(), events: sim.failures }),
      });
      const body = await resp.json();
      if (!resp.ok) throw new Error(JSON.stringify(body.detail || resp.status));
      sim.data = body;
    } catch (err) {
      $("cbba-sim-status").textContent = serverDown(err);
      return;
    }
    view = "sim";
    sim.t = t;
    const end = sim.data.frames[sim.data.frames.length - 1].t;
    $("cbba-sim-time").max = String(end);
    renderAll();
    if (!keepTime) fitAll();
    playSim();
  }

  function playSim() {
    if (!sim.data) return;
    if (sim.t >= sim.data.frames[sim.data.frames.length - 1].t) sim.t = 0;
    sim.running = true;
    sim.lastTs = null;
    sim.raf = requestAnimationFrame(simTick);
    renderSim();
  }
  function stopSim() {
    sim.running = false;
    if (sim.raf) cancelAnimationFrame(sim.raf);
    sim.raf = null;
  }
  function simTick(ts) {
    if (!sim.running) return;
    // A drone is taking a picture: hold the clock until it arrives.
    if (cameraBusy()) {
      sim.lastTs = ts;
      renderSim();
      sim.raf = requestAnimationFrame(simTick);
      return;
    }
    // Slow down while the drones are talking (planning), so it can be seen.
    const now = cbbaFrameAt(sim.data.frames, sim.t);
    const talking = now && now.drones.some((d) => d.status === "planning" || d.status === "holding");
    const speed = talking ? Math.min(sim.speed, 2) : sim.speed;
    if (sim.lastTs != null) sim.t += ((ts - sim.lastTs) / 1000) * speed;
    sim.lastTs = ts;
    const end = sim.data.frames[sim.data.frames.length - 1].t;
    if (sim.t >= end) {
      sim.t = end;
      sim.running = false;
    }
    renderMap();
    renderSim();
    if (sim.running) sim.raf = requestAnimationFrame(simTick);
  }

  async function failNow() {
    const who = $("cbba-fail-select").value;
    if (!who || !sim.data) return;
    sim.failures = [...sim.failures, { at_s: Math.round(sim.t), kind: "drone_lost", drone_id: who }];
    renderFailures();
    // Same seed: everything up to now replays identically, then diverges.
    await runSim(true);
  }

  function addFailure() {
    const kind = $("cbba-fail-kind").value;
    const ev = { at_s: Math.max(0, Number($("cbba-fail-at").value) || 0), kind, drone_id: $("cbba-fail-drone").value };
    if (!ev.drone_id) return;
    if (kind === "battery_drop") ev.battery_pct = Math.min(100, Math.max(0, Number($("cbba-fail-battery").value) || 20));
    sim.failures = [...sim.failures, ev].sort((a, b) => a.at_s - b.at_s);
    renderFailures();
  }

  function renderFailures() {
    const ids = state.drones.map((d) => d.id);
    for (const id of ["cbba-fail-drone", "cbba-fail-select"]) {
      const sel = $(id);
      if (sel.dataset.list !== ids.join(",")) {
        sel.innerHTML = ids.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
        sel.dataset.list = ids.join(",");
      }
    }
    $("cbba-fail-battery-wrap").hidden = $("cbba-fail-kind").value !== "battery_drop";
    sim.failures = sim.failures.filter((f) => ids.includes(f.drone_id));
    $("cbba-fail-list").innerHTML = sim.failures.length
      ? sim.failures.map((f, i) => `<li>at ${f.at_s} s: <b>${esc(f.drone_id)}</b> ${esc(FAILURE_TEXT[f.kind])}${f.kind === "battery_drop" ? ` ${f.battery_pct}%` : ""}
          <button class="cbba-x" data-i="${i}" title="Remove">×</button></li>`).join("")
      : '<li class="cbba-empty">Nothing goes wrong (yet).</li>';
  }

  function renderSim() {
    const has = !!sim.data;
    $("btn-cbba-sim-play").disabled = !has;
    $("btn-cbba-sim-play").textContent = sim.running ? "Pause" : "Play";
    $("btn-cbba-fail").disabled = !has;
    const status = $("cbba-sim-status");
    if (!has) {
      status.textContent = "Set the radio and what goes wrong, then press Run. Each drone runs its own loop: it sends heartbeats, bids, notices silent teammates and re-plans.";
      $("cbba-fly-log").innerHTML = "";
      renderCamera();
      return;
    }
    cameraTick();
    renderCamera();
    const d = sim.data;
    const end = d.frames[d.frames.length - 1].t;
    const frame = cbbaFrameAt(d.frames, sim.t);
    const done = Object.keys(cbbaInspectorsUpTo(d.events, sim.t)).length;
    $("cbba-sim-time").value = String(sim.t);
    status.innerHTML =
      `<b>Mission time ${Math.round(sim.t)} s</b> of ${Math.round(end)} s · ${done} place(s) inspected · ` +
      frame.drones.map((x) => `<span class="cbba-dot" style="background:${color(x.id)}"></span>${esc(x.id)}: ${esc(CBBA_STATUS_TEXT[x.status] || x.status)} (${Math.round(x.battery)}%)`).join(" · ") +
      `<div class="cbba-label">${esc(d.label)}</div>`;
    const shown = d.events.filter((e) => e.t <= sim.t);
    $("cbba-fly-log").innerHTML = shown.slice().reverse().map((e) => `<li class="cbba-ev cbba-ev--${esc(e.kind)}">t=${e.t} s · ${esc(e.text)}</li>`).join("");
    if (sim.t >= end && !sim.running) {
      const s = d.summary;
      const notDone = Object.entries(s.not_inspected).map(([p, why]) => `${cbbaShortName(p)} (${why})`).join("; ");
      $("cbba-fly-log").insertAdjacentHTML("afterbegin",
        `<li class="cbba-summary"><b>Finished:</b> ${s.inspected_count} of ${s.places} place(s) inspected in ${s.duration_s} s` +
        `${s.double_inspected.length ? `; done twice: ${esc(s.double_inspected.join(", "))}` : ""}` +
        `${notDone ? `; not inspected: ${esc(notDone)}` : ""}. Planning rounds: ${s.planning_rounds}. ` +
        `Messages: ${s.messages_sent} sent, ${s.messages_lost} lost. False alarms: ${s.false_alarms}.</li>`);
    }
  }

  // ---- example & reset --------------------------------------------------------
  // A whole scenario from the server (example or quick survey) -> editor.
  function useScenario(sc) {
    state.drones = sc.drones.map((d) => ({
      id: d.drone_id, lat: d.start.lat, lon: d.start.lon, battery: Math.round(d.battery_pct), caps: d.capabilities, offline: false, radioBroken: false,
    }));
    state.points = sc.points.map((p) => ({
      id: p.point_id, lat: p.lat, lon: p.lon, sensor: p.required_capability || "", deadline: p.deadline_s || "", hover: p.inspect_duration_s || 0, value: p.value,
    }));
    state.fence = sc.geofence.points.map((p) => ({ lat: p.lat, lon: p.lon }));
    state.radioMode = "all";
    state.missionId = null;
    edited();
  }

  async function loadExample() {
    stopSim();
    try {
      const resp = await fetch(`${CBBA_BASE}/cbba/sample`);
      const body = await resp.json();
      survey.active = false;
      useScenario(body.scenario);
      fitAll();
      $("cbba-output").textContent = "Example loaded (3 drones, 11 places near Stanford). Click Run CBBA.";
    } catch (err) {
      $("cbba-output").textContent = serverDown(err);
    }
  }

  // READ-ONLY: copy the running fleet's positions/batteries (services/api,
  // via the fleet-agent server) as the drones' start points. Nothing is
  // sent to any vehicle.
  async function importLive() {
    stopSim();
    try {
      const resp = await fetch(`${CBBA_BASE}/fleet/live`);
      const body = await resp.json();
      if (!resp.ok) throw new Error(body.detail || resp.status);
      if (!body.drones.length) {
        $("cbba-output").textContent = "No running drone is reporting a position right now. " + (body.notes || []).join("; ");
        return;
      }
      survey.active = false;
      const old = Object.fromEntries(state.drones.map((d) => [d.id, d]));
      state.drones = body.drones.map((d) => ({
        id: d.id, lat: d.lat, lon: d.lon, battery: Math.round(d.battery),
        caps: (old[d.id] && old[d.id].caps) || ["camera"], offline: false, radioBroken: false,
      }));
      if (body.fence && !state.fence) state.fence = body.fence.map((p) => ({ lat: p.lat, lon: p.lon }));
      edited();
      fitAll();
      $("cbba-output").textContent = `Using ${body.drones.length} running drone(s) from ${body.api}.` + (body.notes && body.notes.length ? " " + body.notes.join("; ") + "." : "");
    } catch (err) {
      $("cbba-output").textContent = `Could not read your running drones: ${err}. Start the fleet (scripts\\run_fleet.ps1) and the CBBA server first.`;
    }
  }

  function clearAll() {
    if ((state.drones.length || state.points.length) && !confirm("Remove all drones, places and the fence?")) return;
    state = { drones: [], points: [], fence: null, radioMode: state.radioMode, rangeM: state.rangeM, missionId: null };
    survey.active = false;
    edited();
    $("cbba-output").textContent = "Cleared. Search for any city above the map, then add drones and places.";
  }

  // ---- quick survey: one line -> mission -> plan -> flight -> camera -----------------
  function setSurveyHeader(html) {
    survey.header = html;
    survey.shownKey = "";
    renderCamera();
  }

  async function goSurvey() {
    const text = $("cbba-survey-text").value.trim();
    if (!text) {
      setSurveyHeader('<p class="cbba-line">Type one line first, e.g. <i>3 drones survey this area</i>.</p>');
      return;
    }
    if (clickMode) setClickMode(clickMode);
    const b = map.getBounds();
    setSurveyHeader('<p class="cbba-line">Reading your line…</p>');
    let body;
    try {
      const resp = await fetch(`${CBBA_BASE}/survey/plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, view: cbbaMapView(b.getSouth(), b.getWest(), b.getNorth(), b.getEast()), engine: $("cbba-survey-engine").value }),
      });
      if (resp.status === 404) {
        setSurveyHeader(
          '<p class="cbba-line cbba-issues">The CBBA server on port 8020 is an old copy (started before you pulled the new code), so it has no quick survey yet. ' +
          "Close its window and start it again: <code>powershell -ExecutionPolicy Bypass -File scripts\\run_all.ps1 -Webcam</code></p>"
        );
        return;
      }
      body = await resp.json();
      if (!resp.ok) {
        const why = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail || resp.status;
        setSurveyHeader(`<p class="cbba-line">The server could not use that: ${esc(why)}. Move the map a little and try again.</p>`);
        return;
      }
    } catch (err) {
      setSurveyHeader(`<p class="cbba-line">${esc(serverDown(err)).replace(/\n/g, "<br>")}</p>`);
      return;
    }
    if (body.question) {
      setSurveyHeader(`<p class="cbba-line">${esc(body.question)}</p>`);
      return;
    }
    survey.active = true;
    useScenario(body.scenario);
    const intro =
      `<div class="cbba-label">Understood by: ${esc(body.provider)}</div>` +
      `<p class="cbba-line">${body.scenario.drones.length} drones, ${body.scenario.points.length} survey points (S01…) over ` +
      `${esc(body.area)} in the middle of your map.</p>`;
    setSurveyHeader(intro + '<p class="cbba-line">The drones are talking to decide who goes where…</p>');
    await runPlan({ instruction: text });
    if (!last) {
      setSurveyHeader(intro + '<p class="cbba-line">Planning did not run - see "2 · Plan with CBBA" below.</p>');
      return;
    }
    if (!last.result.dispatch_allowed) {
      setSurveyHeader(intro + '<p class="cbba-line"><b>The safety check blocked the plan</b> - the reasons are in "2 · Plan with CBBA" below.</p>');
      return;
    }
    setSurveyHeader(
      intro +
      `<p class="cbba-line">✓ ${esc(cbbaAgreementText(last.result))} Safety check passed. Now flying - watch the map: ` +
      "orange lines = drones sending each other bids, 📷 = what each camera saw. You can still type a change in " +
      '"Tell the drones" (e.g. <i>D2 is offline</i>) or make a drone fail in "4 · Live agents".</p>'
    );
    await runSim(false);
  }

  function clearCamera() {
    survey.camera = {};
    survey.gen = (survey.gen || 0) + 1;
    survey.shownKey = "";
    camLayer.clearLayers();
  }

  // When a drone reaches a point, ask the camera for a picture taken NOW
  // (once per point). The playback waits until it arrives (simTick).
  function cameraTick() {
    const pts = new Set(state.points.map((p) => p.id));
    for (const ins of cbbaInspections(sim.data.events)) {
      if (ins.t > sim.t) break;
      const key = `${ins.point}|${ins.drone}`;
      if (survey.camera[key] || !pts.has(ins.point)) continue;
      survey.camera[key] = "pending";
      fetchCamera(key, ins, survey.gen);
    }
  }
  const cameraBusy = () => Object.values(survey.camera).includes("pending");

  async function fetchCamera(key, ins, gen) {
    let r;
    try {
      const resp = await fetch(`${CBBA_BASE}/survey/camera`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ drone_id: ins.drone, point_id: ins.point }),
      });
      r = await resp.json();
      if (!resp.ok) throw new Error(resp.status);
    } catch (err) {
      r = { source: "unavailable", counts: {}, image: null, note: `Could not reach the CBBA server for the camera (${err}).` };
    }
    if (gen !== survey.gen) return; // a newer flight replaced this one
    survey.camera[key] = r;
    renderCamera();
  }

  // Only ever a JPEG data URL from our own server.
  const safeImg = (src) => (typeof src === "string" && /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(src) ? src : null);

  function renderCamera() {
    const inFlight = view === "sim" && sim.data;
    const shown = inFlight ? cbbaInspections(sim.data.events).filter((i) => i.t <= sim.t) : [];
    const res = (i) => survey.camera[`${i.point}|${i.drone}`];
    const ready = shown.filter((i) => res(i) && res(i) !== "pending");
    const waiting = shown.filter((i) => res(i) === "pending");
    const end = inFlight ? sim.data.frames[sim.data.frames.length - 1].t : 0;
    const ended = inFlight && sim.t >= end && !sim.running;
    const key = `${ready.map((i) => i.point).join(",")}#${waiting.map((i) => i.point).join(",")}#${ended}#${inFlight ? 1 : 0}`;
    if (key === survey.shownKey) return;
    survey.shownKey = key;

    camLayer.clearLayers();
    const pts = Object.fromEntries(state.points.map((p) => [p.id, p]));
    const rows = [];
    ready.forEach((i, idx) => {
      const r = res(i);
      const p = pts[i.point];
      if (!p) return;
      const name = cbbaShortName(i.point);
      const off = r.source === "unavailable";
      const what = off ? "camera not connected" : cbbaCountsText(r.counts);
      const total = off ? "✕" : Object.values(r.counts).reduce((a, b) => a + b, 0);
      const img = safeImg(r.image);
      const newest = idx === ready.length - 1 && !ended;
      L.marker([p.lat, p.lon], {
        icon: L.divIcon({ className: "cbba-cam-wrap", html: `<div class="cbba-cam${off ? " cbba-cam--off" : ""}">📷 ${total}</div>`, iconSize: [40, 16], iconAnchor: [-12, 26] }),
        bubblingMouseEvents: false,
      })
        .bindTooltip(`${img ? `<img class="cbba-cam-thumb" src="${img}" alt=""><br>` : ""}<b>${esc(name)}</b> (${esc(i.drone)}): ${esc(what)}`,
          { permanent: newest, direction: "top" })
        .addTo(camLayer);
      rows.push(`<li>${img ? `<img class="cbba-cam-mini" src="${img}" alt="">` : ""}<span class="cbba-dot" style="background:${color(i.drone)}"></span>` +
        `<b>${esc(name)}</b> (${esc(i.drone)}, t=${i.t} s): ${esc(what)}</li>`);
    });

    const parts = [survey.header || ""];
    if (waiting.length) {
      parts.push(`<p class="cbba-line cbba-cam-wait">📷 ${waiting.map((i) => `${esc(i.drone)} is taking a picture at ${esc(cbbaShortName(i.point))}`).join("; ")}… (flight paused)</p>`);
    }
    if (ready.length) {
      const latest = res(ready[ready.length - 1]);
      const latestImg = safeImg(latest.image);
      const real = ready.map(res).filter((r) => r.source !== "unavailable");
      if (ended) {
        if (!real.length) {
          parts.push('<div class="cbba-summary cbba-line"><b>Survey finished</b>, but no camera was connected, so nothing was detected.</div>');
        } else {
          const totals = cbbaSumCounts(real.map((r) => r.counts));
          parts.push(
            `<div class="cbba-summary cbba-line"><b>Survey finished:</b> ${real.length} of ${state.points.length} place(s) photographed. ` +
            `Detected in total: ${esc(cbbaCountsText(totals))}.</div>`
          );
        }
      }
      if (latest.source === "unavailable") {
        parts.push(`<p class="cbba-line cbba-issues">📷 ${esc(latest.note)}</p>`);
      } else {
        const i = ready[ready.length - 1];
        parts.push(`<div class="perception-heading">Latest picture - ${esc(cbbaShortName(i.point))} (${esc(i.drone)})</div>` +
          (latestImg ? `<img class="cbba-cam-big" src="${latestImg}" alt="picture with detection boxes">` : "") +
          `<p class="cbba-line"><b>${esc(cbbaCountsText(latest.counts))}</b></p><p class="cbba-line cbba-muted">${esc(latest.note)}</p>`);
      }
      parts.push(`<div class="perception-heading">All pictures (newest first)</div><ul class="cbba-list cbba-cam-list">${rows.reverse().join("")}</ul>`);
    }
    $("cbba-survey-output").innerHTML = parts.join("");
  }

  // ---- "tell the drones" (instructions) -----------------------------------------
  async function refreshAiStatus() {
    try {
      const s = await (await fetch(`${CBBA_BASE}/ai/status`)).json();
      $("cbba-ai-status").textContent = s.groq_configured
        ? `AI: Groq key found${s.crewai_installed ? ", CrewAI installed" : ""}. If Groq can't be reached, the built-in rules take over.`
        : "AI: no Groq key set, so the built-in rules understand your instruction (works offline).";
    } catch (e) {
      $("cbba-ai-status").textContent = "";
    }
  }

  async function understand() {
    const text = $("cbba-instruct-text").value.trim();
    const out = $("cbba-instruct-output");
    proposal = null;
    $("btn-cbba-apply").disabled = true;
    if (!text) {
      out.textContent = "Type an instruction first.";
      return;
    }
    const problems = cbbaCheckState(state);
    if (problems.length) {
      out.textContent = problems.join(" ");
      return;
    }
    out.textContent = "Reading your instruction…";
    try {
      const resp = await fetch(`${CBBA_BASE}/cbba/instruct`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario: cbbaBuildScenario(state), text, engine: $("cbba-engine").value }),
      });
      proposal = await resp.json();
      if (!resp.ok) throw new Error(JSON.stringify(proposal.detail || resp.status));
    } catch (err) {
      out.textContent = serverDown(err);
      return;
    }
    const parts = [];
    if (proposal.question) {
      parts.push(`<p class="cbba-line">${esc(proposal.question)}</p>`);
    } else {
      parts.push(`<div class="cbba-label">Understood by: ${esc(proposal.provider)}</div>`);
      if (proposal.accepted.length) {
        parts.push('<div class="perception-heading">Changes that passed the check</div><ul class="cbba-list cbba-ok">');
        for (const a of proposal.accepted) parts.push(`<li>✓ ${esc(a.change)}</li>`);
        parts.push("</ul>");
      }
      if (proposal.rejected.length) {
        parts.push('<div class="perception-heading">Refused by the check</div><ul class="cbba-list cbba-issues">');
        for (const r of proposal.rejected) parts.push(`<li>✗ ${esc(r.change)}: ${esc(r.reason)}</li>`);
        parts.push("</ul>");
      }
      if (proposal.scenario) parts.push('<p class="cbba-line cbba-muted">Nothing has changed yet. Press "Apply and re-plan" to use these changes.</p>');
    }
    out.innerHTML = parts.join("");
    $("btn-cbba-apply").disabled = !(proposal && proposal.scenario);
  }

  async function applyProposal() {
    if (!proposal || !proposal.scenario) return;
    const text = $("cbba-instruct-text").value.trim();
    const changes = proposal.accepted.map((a) => a.change);
    state = cbbaApplyScenario(state, proposal.scenario);
    proposal = null;
    $("btn-cbba-apply").disabled = true;
    edited();
    $("cbba-instruct-output").innerHTML =
      '<p class="cbba-line">Applied. The drones are re-planning…</p><ul class="cbba-list cbba-ok">' + changes.map((c) => `<li>✓ ${esc(c)}</li>`).join("") + "</ul>";
    await runPlan({ source: "instruction", instruction: text, changes });
    // In a quick survey, fly the changed plan straight away.
    if (survey.active && last && last.result.dispatch_allowed) await runSim(false);
  }

  // ---- CBBA server status (port 8020), checked every 5 s ----------------------------
  async function checkCbbaServer() {
    const el = $("cbba-server-status");
    let ok = false;
    try {
      ok = (await fetch(`${CBBA_BASE}/health`, { cache: "no-store" })).ok;
    } catch (e) {
      ok = false;
    }
    el.className = `cbba-server cbba-server--${ok ? "ok" : "down"}`;
    el.textContent = ok
      ? `CBBA server: connected ✓ (${CBBA_BASE})`
      : `CBBA server: not running at ${CBBA_BASE}. Start everything with: powershell -ExecutionPolicy Bypass -File scripts\\run_all.ps1`;
  }

  // ---- wiring -------------------------------------------------------------------
  function renderAll() {
    renderSetup();
    renderMap();
    if (last) renderResult();
    renderReplay();
    renderFailures();
    renderSim();
    $("btn-cbba-step-show").textContent = view === "replay" ? "Back to plan" : "Start replay";
  }

  $("btn-cbba-add-drone").addEventListener("click", () => setClickMode("drone"));
  $("btn-cbba-add-point").addEventListener("click", () => setClickMode("point"));
  $("btn-cbba-draw-fence").addEventListener("click", () => (clickMode === "fence" ? finishFence() : setClickMode("fence")));
  $("btn-cbba-auto-fence").addEventListener("click", () => {
    state.fence = null;
    edited();
  });
  $("btn-cbba-example").addEventListener("click", loadExample);
  $("btn-cbba-clear-all").addEventListener("click", clearAll);
  $("btn-cbba-import-live").addEventListener("click", importLive);
  $("cbba-radio-mode").addEventListener("change", (e) => {
    state.radioMode = e.target.value;
    edited();
  });
  $("cbba-range").addEventListener("change", (e) => {
    state.rangeM = Math.max(1, Number(e.target.value) || 300);
    edited();
  });
  for (const id of ["cbba-drone-list", "cbba-point-list"]) {
    $(id).addEventListener("change", onListChange);
    $(id).addEventListener("click", onListClick);
  }
  $("btn-cbba-run").addEventListener("click", () => runPlan());
  $("btn-cbba-step-show").addEventListener("click", startReplay);
  $("btn-cbba-step-prev").addEventListener("click", () => stepBy(-1));
  $("btn-cbba-step-next").addEventListener("click", () => stepBy(1));
  $("btn-cbba-fly").addEventListener("click", () => runSim(false));
  $("btn-cbba-sim-play").addEventListener("click", () => (sim.running ? (stopSim(), renderSim()) : playSim()));
  $("cbba-fly-speed").addEventListener("change", (e) => (sim.speed = Number(e.target.value)));
  $("cbba-sim-time").addEventListener("input", (e) => {
    stopSim();
    sim.t = Number(e.target.value);
    renderMap();
    renderSim();
  });
  $("btn-cbba-fail").addEventListener("click", failNow);
  $("btn-cbba-fail-add").addEventListener("click", addFailure);
  $("cbba-fail-kind").addEventListener("change", renderFailures);
  $("cbba-fail-list").addEventListener("click", (e) => {
    if (e.target.dataset.i === undefined) return;
    sim.failures.splice(Number(e.target.dataset.i), 1);
    renderFailures();
  });
  $("btn-cbba-survey-go").addEventListener("click", goSurvey);
  $("cbba-survey-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      goSurvey();
    }
  });
  $("btn-cbba-understand").addEventListener("click", understand);
  $("btn-cbba-apply").addEventListener("click", applyProposal);
  $("cbba-instruct-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) understand();
  });
  refreshAiStatus();
  checkCbbaServer();
  setInterval(checkCbbaServer, 5000);

  const hadSaved = load();
  renderAll();
  if (!hadSaved) {
    fetch(`${CBBA_BASE}/health`).then((r) => r.ok && loadExample()).catch(() => {
      $("cbba-output").textContent = serverDown("server not running");
    });
  } else if (state.drones.length || state.points.length) {
    $("cbba-output").textContent = "Your last mission was restored. Click Run CBBA.";
  }
})();
