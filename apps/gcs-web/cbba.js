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

// A drone's planned route -> timed legs for the flight simulation.
// Arrival times at points come from the server; the final leg home is
// distance / cruise speed.
function cbbaTimeline(plan, pointsById, speedMs) {
  const legs = [];
  if (!plan.route || plan.route.length < 2) return { legs, end: 0 };
  let t = 0;
  for (let i = 0; i < plan.path.length; i++) {
    const from = plan.route[i], to = plan.route[i + 1];
    const arrive = plan.arrival_s[i];
    legs.push({ from, to, t0: t, t1: arrive, point: plan.path[i] });
    t = arrive + Number((pointsById[plan.path[i]] || {}).inspect_duration_s || 0);
  }
  const last = plan.route[plan.route.length - 2], home = plan.route[plan.route.length - 1];
  const back = cbbaDistanceM(last, home) / speedMs;
  legs.push({ from: last, to: home, t0: t, t1: t + back, point: null });
  return { legs, end: t + back };
}

function cbbaPositionAt(timeline, t) {
  const { legs } = timeline;
  if (legs.length === 0) return null;
  if (t <= 0) return { lat: legs[0].from.lat, lon: legs[0].from.lon };
  for (const leg of legs) {
    if (t <= leg.t0) return { lat: leg.from.lat, lon: leg.from.lon }; // hovering before this leg
    if (t <= leg.t1) {
      const f = leg.t1 > leg.t0 ? (t - leg.t0) / (leg.t1 - leg.t0) : 1;
      return { lat: leg.from.lat + (leg.to.lat - leg.from.lat) * f, lon: leg.from.lon + (leg.to.lon - leg.from.lon) * f };
    }
  }
  const end = legs[legs.length - 1].to;
  return { lat: end.lat, lon: end.lon };
}

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

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    cbbaDroneColor, cbbaDistanceM, cbbaAutoFence, cbbaLinks, cbbaBuildScenario, cbbaCheckState,
    cbbaPointStatuses, cbbaClaimOwners, cbbaReplaySteps, cbbaEventText, cbbaTimeline, cbbaPositionAt,
    cbbaGroupIssues, cbbaAgreementText, cbbaEscape, cbbaShortName, CBBA_COLORS,
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
  const $ = (id) => document.getElementById(id);
  const esc = cbbaEscape;

  // ---- state -------------------------------------------------------------
  let state = { drones: [], points: [], fence: null, radioMode: "all", rangeM: 300 };
  let clickMode = null; // "drone" | "point" | "fence"
  let fenceDraft = [];
  let view = "edit"; // "edit" | "plan" | "replay" | "fly"
  let last = null; // last server response {scenario, result, reference, trace}
  let steps = [];
  let stepIdx = 0;
  const fly = { running: false, t: 0, offset: 0, speed: 10, inspected: new Set(), inspectedBy: {}, failed: {}, timelines: {}, raf: null, lastTs: null, log: [] };

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
    stopFlight();
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
    const cls = "cbba-drone" + (opts.grey ? " cbba-drone--off" : "") + (opts.failed ? " cbba-drone--failed" : "");
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

    // Routes
    if (result && (view === "plan" || view === "fly")) {
      for (const plan of result.plans) {
        if (!plan.route.length) continue;
        L.polyline(plan.route.map((w) => [w.lat, w.lon]), {
          color: color(plan.drone_id), weight: view === "fly" ? 3 : 4, opacity: view === "fly" ? 0.35 : 0.85, ...noBubble,
        }).bindTooltip(`${plan.drone_id}: ${Math.round(plan.route_m)} m at ${plan.altitude_m} m altitude`).addTo(layer);
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
    if (result && (view === "plan" || view === "fly")) statuses = cbbaPointStatuses(last.scenario, result);
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
      if (view === "fly" && fly.inspected.has(p.id)) {
        fill = color(fly.inspectedBy[p.id]);
        extra = " cbba-point--done";
        tip += "<br>✓ inspected";
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
      if (view === "fly" && fly.timelines[d.id]) at = cbbaPositionAt(fly.timelines[d.id], fly.t) || at;
      if (view === "fly" && fly.failed && fly.failed[d.id]) {
        at = fly.failed[d.id];
        failed = true;
      }
      const grey = d.offline || failed;
      const tip = `${esc(d.id)}${d.offline ? " (OFFLINE)" : ""}${failed ? " (FAILED)" : ""}: ${esc(d.caps.join(" + ") || "no sensors")}, ` +
        `battery ${Math.round(d.battery)}%${d.radioBroken ? ", radio broken" : ""}`;
      const m = L.marker([at.lat, at.lon], { icon: droneIcon(d.id, { grey, failed }), draggable: editable, ...noBubble }).bindTooltip(tip);
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

  async function postPlan(scenario) {
    const resp = await fetch(`${CBBA_BASE}/cbba/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(scenario),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail || resp.status;
      throw new Error(`the server rejected the mission: ${detail}`);
    }
    return body;
  }

  async function runPlan() {
    if (clickMode) setClickMode(clickMode);
    stopFlight();
    const problems = cbbaCheckState(state);
    if (problems.length) {
      $("cbba-output").innerHTML = '<ul class="cbba-list cbba-issues">' + problems.map((p) => `<li>${esc(p)}</li>`).join("") + "</ul>";
      return;
    }
    $("cbba-output").textContent = "The drones are planning…";
    try {
      last = await postPlan(cbbaBuildScenario(state));
    } catch (err) {
      $("cbba-output").textContent = String(err).includes("rejected") ? String(err) : serverDown(err);
      return;
    }
    steps = cbbaReplaySteps(last.trace || []);
    stepIdx = 0;
    view = "plan";
    fly.inspected = new Set();
    fly.log = [];
    fly.failed = {};
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
    stopFlight();
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

  // ---- simulated flight -------------------------------------------------------
  function buildTimelines() {
    const pointsById = Object.fromEntries(last.scenario.points.map((p) => [p.point_id, p]));
    const speed = last.scenario.constraints.cruise_speed_ms;
    fly.timelines = {};
    for (const plan of last.result.plans) fly.timelines[plan.drone_id] = cbbaTimeline(plan, pointsById, speed);
    // After a re-plan, a drone with nothing left to inspect still flies home.
    for (const d of last.scenario.drones) {
      const tl = fly.timelines[d.drone_id];
      if (d.health === "OFFLINE" || !d.home || (tl && tl.legs.length)) continue;
      const back = cbbaDistanceM(d.start, d.home) / speed;
      if (back > 0) fly.timelines[d.drone_id] = { legs: [{ from: d.start, to: d.home, t0: 0, t1: back, point: null }], end: back };
    }
  }
  const flightEnd = () => Math.max(0, ...Object.values(fly.timelines).map((tl) => tl.end));

  function logFlight(text) {
    fly.log.push(`t=${Math.round(fly.offset + fly.t)} s  ${text}`);
  }

  function startFlight() {
    if (!last || !last.result.dispatch_allowed) return;
    if (view !== "fly") {
      view = "fly";
      fly.t = 0;
      fly.offset = 0;
      fly.inspected = new Set();
      fly.inspectedBy = {};
      fly.failed = {};
      fly.log = [];
      buildTimelines();
      logFlight("All drones take off (simulated).");
    }
    fly.running = true;
    fly.lastTs = null;
    fly.raf = requestAnimationFrame(tick);
    renderAll();
  }
  function stopFlight() {
    fly.running = false;
    if (fly.raf) cancelAnimationFrame(fly.raf);
    fly.raf = null;
  }

  function tick(ts) {
    if (!fly.running) return;
    if (fly.lastTs != null) fly.t += ((ts - fly.lastTs) / 1000) * fly.speed;
    fly.lastTs = ts;
    for (const plan of last.result.plans) {
      plan.path.forEach((pid, k) => {
        if (!fly.inspected.has(pid) && !(fly.failed && fly.failed[plan.drone_id]) && fly.t >= plan.arrival_s[k]) {
          fly.inspected.add(pid);
          fly.inspectedBy[pid] = plan.drone_id;
          logFlight(`${plan.drone_id} inspected ${pid}.`);
        }
      });
    }
    if (fly.t >= flightEnd()) {
      fly.t = flightEnd();
      fly.running = false;
      const total = state.points.length;
      logFlight(`All drones are back home. ${fly.inspected.size} of ${total} place(s) inspected.`);
    }
    renderMap();
    renderFlight();
    if (fly.running) fly.raf = requestAnimationFrame(tick);
  }

  async function failDrone() {
    const who = $("cbba-fail-select").value;
    if (!who || view !== "fly") return;
    stopFlight();
    const T = fly.t;
    const drain = last.scenario.constraints.battery_drain_pct_per_min;
    const pos = {};
    for (const d of state.drones) pos[d.id] = (fly.timelines[d.id] && cbbaPositionAt(fly.timelines[d.id], T)) || { lat: d.lat, lon: d.lon };
    fly.failed = { ...(fly.failed || {}), [who]: pos[who] };
    const lost = (last.result.plans.find((p) => p.drone_id === who) || { path: [] }).path.filter((p) => !fly.inspected.has(p));
    logFlight(`${who} FAILED. Places it had not reached yet: ${lost.length ? lost.join(", ") : "none"}.`);
    logFlight("The other drones re-plan with CBBA from where they are now (deterministic code, no AI).");
    renderFlight();

    // New mission: everyone starts where they are now, must still get home,
    // with less battery; failed drones are offline; inspected places are done.
    const scenario = cbbaBuildScenario(state);
    const used = (drain * (fly.offset + T)) / 60; // battery used since take-off
    scenario.drones = scenario.drones.map((d) => {
      const isFailed = !!fly.failed[d.drone_id];
      return {
        ...d,
        start: { lat: pos[d.drone_id].lat, lon: pos[d.drone_id].lon, alt_m: 0 },
        home: d.home || { lat: d.start.lat, lon: d.start.lon, alt_m: 0 },
        battery_pct: Math.max(0, d.battery_pct - (isFailed ? 0 : used)),
        health: isFailed || d.health === "OFFLINE" ? "OFFLINE" : "OK",
      };
    });
    scenario.points = scenario.points.filter((p) => !fly.inspected.has(p.point_id));
    if (!scenario.points.length) {
      logFlight("Nothing left to inspect.");
      renderFlight();
      return;
    }
    let resp;
    try {
      resp = await postPlan(scenario);
    } catch (err) {
      logFlight(`Re-plan failed: ${err}`);
      renderFlight();
      return;
    }
    const moved = [];
    for (const plan of resp.result.plans) for (const pid of plan.path) if (lost.includes(pid)) moved.push(`${pid} → ${plan.drone_id}`);
    logFlight(`New plan: ${moved.length ? moved.join(", ") : "nobody could take the lost places"}. Safety gate: ${resp.result.dispatch_allowed ? "PASSED" : "BLOCKED"}.`);
    if (!resp.result.dispatch_allowed) {
      logFlight("The new plan is blocked, so the simulation stops here. (A real system would hold or return the drones home.)");
      renderFlight();
      return;
    }
    // Continue flying the new plan; keep the original setup for the map.
    fly.offset += T;
    fly.t = 0;
    last = { ...resp, scenario: { ...resp.scenario, points: last.scenario.points } };
    buildTimelines();
    fly.running = true;
    fly.lastTs = null;
    fly.raf = requestAnimationFrame(tick);
  }

  function renderFlight() {
    const ok = last && last.result.dispatch_allowed;
    $("btn-cbba-fly").disabled = !ok;
    $("btn-cbba-fly").textContent = view === "fly" && fly.running ? "Pause" : view === "fly" ? "Continue" : "Fly (simulated)";
    $("btn-cbba-fail").disabled = !(view === "fly");
    const sel = $("cbba-fail-select");
    const alive = state.drones.filter((d) => !d.offline && !(fly.failed && fly.failed[d.id])).map((d) => d.id);
    if (sel.dataset.list !== alive.join(",")) {
      sel.innerHTML = alive.map((id) => `<option value="${esc(id)}">${esc(id)}</option>`).join("");
      sel.dataset.list = alive.join(",");
    }
    const status = $("cbba-fly-status");
    if (!last) status.textContent = "Plan the mission first.";
    else if (!ok) status.textContent = "Only a plan that PASSED the safety gate can be flown.";
    else if (view !== "fly") status.textContent = "Ready. The drones will follow their planned routes (simulation).";
    else status.textContent = `Mission time ${Math.round(fly.offset + fly.t)} s · ${fly.inspected.size} of ${state.points.length} place(s) inspected`;
    $("cbba-fly-log").innerHTML = fly.log.slice().reverse().map((l) => `<li>${esc(l)}</li>`).join("");
  }

  function toggleFly() {
    if (view === "fly" && fly.running) {
      stopFlight();
      renderFlight();
    } else {
      startFlight();
    }
  }

  // ---- example & reset --------------------------------------------------------
  async function loadExample() {
    stopFlight();
    try {
      const resp = await fetch(`${CBBA_BASE}/cbba/sample`);
      const body = await resp.json();
      const sc = body.scenario;
      state.drones = sc.drones.map((d) => ({
        id: d.drone_id, lat: d.start.lat, lon: d.start.lon, battery: Math.round(d.battery_pct), caps: d.capabilities, offline: false, radioBroken: false,
      }));
      state.points = sc.points.map((p) => ({
        id: p.point_id, lat: p.lat, lon: p.lon, sensor: p.required_capability || "", deadline: p.deadline_s || "", hover: p.inspect_duration_s || 0, value: p.value,
      }));
      state.fence = sc.geofence.points.map((p) => ({ lat: p.lat, lon: p.lon }));
      state.radioMode = "all";
      edited();
      fitAll();
      $("cbba-output").textContent = "Example loaded (3 drones, 11 places near Stanford). Click Run CBBA.";
    } catch (err) {
      $("cbba-output").textContent = serverDown(err);
    }
  }

  function clearAll() {
    if ((state.drones.length || state.points.length) && !confirm("Remove all drones, places and the fence?")) return;
    state = { drones: [], points: [], fence: null, radioMode: state.radioMode, rangeM: state.rangeM };
    edited();
    $("cbba-output").textContent = "Cleared. Search for any city above the map, then add drones and places.";
  }

  // ---- wiring -------------------------------------------------------------------
  function renderAll() {
    renderSetup();
    renderMap();
    if (last) renderResult();
    renderReplay();
    renderFlight();
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
  $("btn-cbba-run").addEventListener("click", runPlan);
  $("btn-cbba-step-show").addEventListener("click", startReplay);
  $("btn-cbba-step-prev").addEventListener("click", () => stepBy(-1));
  $("btn-cbba-step-next").addEventListener("click", () => stepBy(1));
  $("btn-cbba-fly").addEventListener("click", toggleFly);
  $("cbba-fly-speed").addEventListener("change", (e) => (fly.speed = Number(e.target.value)));
  $("btn-cbba-fail").addEventListener("click", failDrone);

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
