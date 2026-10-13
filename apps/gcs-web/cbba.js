// Multi-drone CBBA simulation panel (docs/CBBA_PLAN.md, docs/adr/0018).
//
// Asks services/fleet-agent's read-only server (app/server.py, port 8020)
// to run the CBBA simulation and draws the answer on the map: geofence,
// inspection points coloured by the drone that won them, each drone's
// route, and the safety gate's verdict. It sends no commands and changes
// nothing on any vehicle -- it only displays a simulated plan.
//
// The first half is pure logic (no `document`/`L`), require()-able from
// Node for tests (tests/cbba.test.js), the same pattern as freshness.js.
// The second half only runs in the browser, after app.js (it uses app.js's
// `map` and `params` globals).

const CBBA_COLORS = ["#2563eb", "#d97706", "#059669", "#7c3aed", "#db2777", "#0891b2"];

function cbbaDroneColor(droneId, allDroneIds) {
  const i = [...allDroneIds].sort().indexOf(droneId);
  return i < 0 ? "#94a3b8" : CBBA_COLORS[i % CBBA_COLORS.length];
}

// point_id -> { state, owners, reason }
//   state: "assigned" (one drone), "double" (claimed by 2+ drones -- the
//   safety gate blocks this), "unassigned" (no drone could take it), or
//   "ignored" (outside the geofence).
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

// Safety issues grouped by code, so nine "claimed twice" lines become one:
// [{ code, count, messages: [...] }] in first-seen order.
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

// One plain-English sentence about how the drones reached agreement.
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

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    cbbaDroneColor, cbbaPointStatuses, cbbaEscape, cbbaShortName, cbbaGroupIssues, cbbaAgreementText, CBBA_COLORS,
  };
}

// ---------------------------------------------------------------------------
// Browser only below.

if (typeof document !== "undefined" && typeof L !== "undefined") {
  const CBBA_BASE = params.get("cbba") || "http://localhost:8020";
  const cbbaLayer = L.layerGroup().addTo(map);
  const output = document.getElementById("cbba-output");
  const STATE_FILL = { unassigned: "#ffffff", ignored: "#94a3b8", double: "#dc2626" };

  function cbbaNotRunningHint(err) {
    return (
      `Could not reach the CBBA server on ${CBBA_BASE} (${err}). Start it in a new terminal:\n` +
      "cd services\\fleet-agent\n$env:PYTHONPATH = \".\"\n" +
      "..\\..\\.venv\\Scripts\\python.exe -m uvicorn app.server:app --port 8020"
    );
  }

  function drawCbba(scenario, result) {
    cbbaLayer.clearLayers();
    const ids = scenario.drones.map((d) => d.drone_id);
    const excluded = new Set(result.excluded_drones.map((x) => x.item_id));
    const statuses = cbbaPointStatuses(scenario, result);
    const noBubble = { bubblingMouseEvents: false };

    const fence = scenario.geofence.points.map((p) => [p.lat, p.lon]);
    L.polygon(fence, { color: "#0f172a", weight: 2, dashArray: "6 5", fill: false, interactive: false }).addTo(cbbaLayer);

    for (const plan of result.plans) {
      if (!plan.route.length) continue;
      L.polyline(
        plan.route.map((w) => [w.lat, w.lon]),
        { color: cbbaDroneColor(plan.drone_id, ids), weight: 4, opacity: 0.85, ...noBubble }
      )
        .bindTooltip(`${plan.drone_id} route: ${Math.round(plan.route_m)} m at ${plan.altitude_m} m altitude`)
        .addTo(cbbaLayer);
    }

    for (const p of scenario.points) {
      const st = statuses[p.point_id];
      const fill = st.state === "assigned" ? cbbaDroneColor(st.owners[0], ids) : STATE_FILL[st.state];
      const sensor = p.required_capability ? ` (needs ${p.required_capability})` : "";
      let tip = `<b>${cbbaEscape(p.point_id)}</b>${cbbaEscape(sensor)}<br>`;
      if (st.state === "assigned") {
        const plan = result.plans.find((pl) => pl.drone_id === st.owners[0]);
        const t = plan.arrival_s[plan.path.indexOf(p.point_id)];
        tip += `Inspected by ${cbbaEscape(st.owners[0])}, arrives at ${Math.round(t)} s`;
      } else if (st.state === "double") {
        tip += `Claimed by ${cbbaEscape(st.owners.join(" and "))} -- blocked`;
      } else {
        tip += cbbaEscape(st.reason);
      }
      if (p.deadline_s) tip += `<br>Deadline: ${Math.round(p.deadline_s)} s`;

      const html = `<div class="cbba-point${p.required_capability ? " cbba-point--sensor" : ""}" style="background:${fill}">${cbbaEscape(cbbaShortName(p.point_id))}</div>`;
      L.marker([p.lat, p.lon], {
        icon: L.divIcon({ className: "cbba-point-wrap", html, iconSize: [34, 20], iconAnchor: [17, 10] }),
        ...noBubble,
      })
        .bindTooltip(tip)
        .addTo(cbbaLayer);
    }

    for (const d of scenario.drones) {
      const off = excluded.has(d.drone_id);
      const color = off ? "#94a3b8" : cbbaDroneColor(d.drone_id, ids);
      const html = `<div class="cbba-drone${off ? " cbba-drone--off" : ""}" style="color:${color}">▲<span>${cbbaEscape(d.drone_id)}</span></div>`;
      L.marker([d.start.lat, d.start.lon], {
        icon: L.divIcon({ className: "cbba-drone-wrap", html, iconSize: [24, 24], iconAnchor: [12, 12] }),
        ...noBubble,
      })
        .bindTooltip(
          `${cbbaEscape(d.drone_id)}${off ? " (OFFLINE)" : ""}: ${cbbaEscape(d.capabilities.join(" + ") || "no sensors")}, battery ${Math.round(d.battery_pct)}%`
        )
        .addTo(cbbaLayer);
    }

    map.fitBounds(L.latLngBounds(fence).pad(0.15));
  }

  function renderCbbaPanel(body) {
    const { scenario, result, reference } = body;
    const ids = scenario.drones.map((d) => d.drone_id);
    const caps = Object.fromEntries(scenario.drones.map((d) => [d.drone_id, d.capabilities.join("+") || "no sensors"]));
    const parts = [];

    parts.push(
      result.dispatch_allowed
        ? '<div class="cbba-verdict cbba-verdict--ok">Safety gate: PASSED</div>'
        : '<div class="cbba-verdict cbba-verdict--blocked">Safety gate: BLOCKED</div>'
    );
    parts.push(`<div class="cbba-label">${cbbaEscape(result.label)} &middot; simulation only, nothing is flown</div>`);

    parts.push(`<p class="cbba-line">${cbbaEscape(cbbaAgreementText(result))}</p>`);

    parts.push('<div class="perception-heading">Who inspects what</div><ul class="cbba-list">');
    for (const plan of result.plans) {
      const dot = `<span class="cbba-dot" style="background:${cbbaDroneColor(plan.drone_id, ids)}"></span>`;
      const route = plan.path.length ? plan.path.map(cbbaShortName).map(cbbaEscape).join(" → ") : "nothing";
      parts.push(
        `<li>${dot}<b>${cbbaEscape(plan.drone_id)}</b> <span class="cbba-muted">(${cbbaEscape(caps[plan.drone_id])}, ${plan.altitude_m} m)</span><br>${route}</li>`
      );
    }
    for (const x of result.excluded_drones) {
      parts.push(`<li><span class="cbba-dot" style="background:#94a3b8"></span><b>${cbbaEscape(x.item_id)}</b> <span class="cbba-muted">left out: ${cbbaEscape(x.reason)}</span></li>`);
    }
    parts.push("</ul>");

    const notDone = [...result.unassigned, ...result.excluded_points];
    if (notDone.length) {
      parts.push('<div class="perception-heading">Not inspected</div><ul class="cbba-list">');
      for (const x of notDone) parts.push(`<li><b>${cbbaEscape(cbbaShortName(x.item_id))}</b> <span class="cbba-muted">${cbbaEscape(x.reason)}</span></li>`);
      parts.push("</ul>");
    }

    if (result.safety.issues.length) {
      parts.push('<div class="perception-heading">Why it is blocked</div><ul class="cbba-list cbba-issues">');
      for (const g of cbbaGroupIssues(result.safety.issues)) {
        const more = g.count > 1 ? ` <span class="cbba-muted">(and ${g.count - 1} more like this)</span>` : "";
        parts.push(`<li><b>${cbbaEscape(g.code)}</b>: ${cbbaEscape(g.messages[0])}${more}</li>`);
      }
      parts.push("</ul>");
    } else {
      parts.push(
        `<p class="cbba-line cbba-muted">Central reference planner (SGA): ${reference.same_allocation ? "same answer" : "different answer"}.</p>`
      );
    }
    output.innerHTML = parts.join("");
  }

  async function runCbba() {
    const qs = new URLSearchParams({ comms: document.getElementById("cbba-comms").value });
    for (const box of document.querySelectorAll(".cbba-offline:checked")) qs.append("offline", box.value);
    output.textContent = "Running CBBA…";
    try {
      const resp = await fetch(`${CBBA_BASE}/cbba/sample?${qs}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      const body = await resp.json();
      drawCbba(body.scenario, body.result);
      renderCbbaPanel(body);
    } catch (err) {
      output.textContent = cbbaNotRunningHint(err);
    }
  }

  function clearCbba() {
    cbbaLayer.clearLayers();
    output.textContent = "Cleared from the map.";
  }

  document.getElementById("btn-cbba-run").addEventListener("click", runCbba);
  document.getElementById("btn-cbba-clear").addEventListener("click", clearCbba);
}
