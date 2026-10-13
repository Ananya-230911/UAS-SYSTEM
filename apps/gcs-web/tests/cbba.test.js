const test = require("node:test");
const assert = require("node:assert/strict");
const { cbbaDroneColor, cbbaPointStatuses, cbbaEscape, cbbaShortName, CBBA_COLORS } = require("../cbba.js");

const scenario = {
  points: [{ point_id: "P01-a" }, { point_id: "P02-b" }, { point_id: "P03-c" }, { point_id: "P04-d" }],
};

test("each drone gets a stable colour by sorted id", () => {
  assert.equal(cbbaDroneColor("sim-1", ["sim-3", "sim-1", "sim-2"]), CBBA_COLORS[0]);
  assert.equal(cbbaDroneColor("sim-3", ["sim-3", "sim-1", "sim-2"]), CBBA_COLORS[2]);
  assert.equal(cbbaDroneColor("ghost", ["sim-1"]), "#94a3b8");
});

test("point states: assigned, claimed twice, unassigned, ignored", () => {
  const result = {
    plans: [
      { drone_id: "sim-1", path: ["P01-a", "P02-b"] },
      { drone_id: "sim-2", path: ["P02-b"] },
    ],
    unassigned: [{ item_id: "P03-c", reason: "no available drone carries a 'lidar' sensor" }],
    excluded_points: [{ item_id: "P04-d", reason: "outside the geofence" }],
  };
  const s = cbbaPointStatuses(scenario, result);
  assert.deepEqual(s["P01-a"], { state: "assigned", owners: ["sim-1"], reason: "" });
  assert.equal(s["P02-b"].state, "double");
  assert.deepEqual(s["P02-b"].owners, ["sim-1", "sim-2"]);
  assert.equal(s["P03-c"].state, "unassigned");
  assert.match(s["P03-c"].reason, /lidar/);
  assert.equal(s["P04-d"].state, "ignored");
});

test("text from the server is escaped before going into HTML", () => {
  assert.equal(cbbaEscape('<img src=x onerror="a">&\''), "&lt;img src=x onerror=&quot;a&quot;&gt;&amp;&#39;");
});

test("short point names", () => {
  assert.equal(cbbaShortName("P04-solar-panels"), "P04");
  assert.equal(cbbaShortName("p7"), "p7");
});

const { cbbaGroupIssues, cbbaAgreementText } = require("../cbba.js");

test("issues are grouped by code with a count", () => {
  const g = cbbaGroupIssues([
    { code: "DUPLICATE_ASSIGNMENT", message: "a" },
    { code: "NOT_CONVERGED", message: "b" },
    { code: "DUPLICATE_ASSIGNMENT", message: "c" },
  ]);
  assert.deepEqual(g, [
    { code: "DUPLICATE_ASSIGNMENT", count: 2, messages: ["a", "c"] },
    { code: "NOT_CONVERGED", count: 1, messages: ["b"] },
  ]);
});

test("agreement sentence: one group, split network, not converged", () => {
  const base = { converged: true, rounds: 4, last_change_round: 3, messages_sent: 24 };
  assert.match(cbbaAgreementText({ ...base, comms_groups: [["a", "b"]] }), /agreed in round 3 using 24 messages/);
  const split = cbbaAgreementText({ ...base, comms_groups: [["a", "b"], ["c"]] });
  assert.match(split, /split: \{a, b\} and \{c\} can't hear each other/);
  assert.match(cbbaAgreementText({ ...base, converged: false }), /did NOT reach agreement within 4 rounds/);
});

const m = require("../cbba.js");
const D = (id, lat, lon, extra = {}) => ({ id, lat, lon, battery: 90, caps: ["camera"], offline: false, radioBroken: false, ...extra });
const P = (id, lat, lon, extra = {}) => ({ id, lat, lon, sensor: "", deadline: "", hover: 0, ...extra });

test("distance matches the server's flat-earth formula (~111 m per 0.001 deg latitude)", () => {
  const d = m.cbbaDistanceM({ lat: 28.6, lon: 77.2 }, { lat: 28.601, lon: 77.2 });
  assert.ok(Math.abs(d - 111.2) < 0.5, String(d));
});

test("automatic fence is a box that contains every drone and place, with margin", () => {
  const f = m.cbbaAutoFence([D("a", 13.08, 80.27)], [P("p", 13.09, 80.28)], 100);
  assert.equal(f.length, 4);
  const lats = f.map((x) => x.lat), lons = f.map((x) => x.lon);
  assert.ok(Math.min(...lats) < 13.08 && Math.max(...lats) > 13.09);
  assert.ok(Math.min(...lons) < 80.27 && Math.max(...lons) > 80.28);
  assert.equal(m.cbbaAutoFence([], []), null);
});

test("radio links: everyone, limited range, broken radio", () => {
  const ds = [D("a", 28.6, 77.2), D("b", 28.601, 77.2), D("c", 28.61, 77.2)]; // a-b ~111 m, a-c ~1.1 km
  assert.deepEqual(m.cbbaLinks(ds, "all", 0), [["a", "b"], ["a", "c"], ["b", "c"]]);
  assert.deepEqual(m.cbbaLinks(ds, "range", 200), [["a", "b"]]);
  ds[1].radioBroken = true;
  assert.deepEqual(m.cbbaLinks(ds, "all", 0), [["a", "c"]]);
});

test("scenario JSON: comms omitted when everyone can talk, explicit links otherwise", () => {
  const state = { drones: [D("a", 28.6, 77.2), D("b", 28.601, 77.2, { offline: true })], points: [P("p1", 28.6005, 77.2005, { sensor: "thermal", deadline: "90" })], fence: null, radioMode: "all", rangeM: 300 };
  let sc = m.cbbaBuildScenario(state);
  assert.deepEqual(sc.comms, {});
  assert.equal(sc.drones[1].health, "OFFLINE");
  assert.deepEqual(sc.points[0], { point_id: "p1", lat: 28.6005, lon: 77.2005, required_capability: "thermal", deadline_s: 90, inspect_duration_s: 0, value: 100 });
  assert.equal(sc.geofence.points.length, 4);
  state.drones[0].radioBroken = true;
  sc = m.cbbaBuildScenario(state);
  assert.deepEqual(sc.comms, { links: [] });
});

test("setup problems are explained before anything is sent", () => {
  const empty = { drones: [], points: [], fence: null };
  assert.equal(m.cbbaCheckState(empty).length, 2);
  const dup = { drones: [D("x", 1, 1)], points: [P("x", 1, 1)], fence: null };
  assert.match(m.cbbaCheckState(dup).join(" "), /different: x/);
  const bad = { drones: [D("a", 1, 1, { battery: 150 })], points: [P("p", 1, 1)], fence: [{ lat: 1, lon: 1 }] };
  const msgs = m.cbbaCheckState(bad).join(" ");
  assert.match(msgs, /battery must be 0-100/);
  assert.match(msgs, /at least 3 corners/);
});

test("replay has two steps per round: thinking alone, then talking", () => {
  const trace = [
    {
      round: 1, links: [["a", "b"]], messages: 2,
      wants: { a: ["p1"], b: ["p1"] }, claims: { a: ["p1"], b: [] },
      events: [
        { kind: "bid", drone: "a", point: "p1", bid: 90 },
        { kind: "bid", drone: "b", point: "p1", bid: 80 },
        { kind: "dropped", drone: "b", point: "p1", bid: 80, reason: "outbid", winner: "a", winner_bid: 90 },
      ],
    },
  ];
  const steps = m.cbbaReplaySteps(trace);
  assert.equal(steps.length, 2);
  assert.equal(steps[0].phase, "bid");
  assert.deepEqual(steps[0].links, []);
  assert.match(steps[0].text, /1 place\(s\) are wanted by more than one drone/);
  assert.equal(steps[1].phase, "talk");
  assert.match(steps[1].text, /2 message\(s\) over 1 radio link/);
  assert.equal(m.cbbaEventText(steps[1].events[0]), "b gives up p1: a bid more (90 vs 80)");
  assert.equal(m.cbbaEventText(steps[0].events[0]), "a bids 90 for p1");
});

test("flight timeline: fly to each place, hover, fly home; position in between", () => {
  const plan = {
    path: ["p1"], arrival_s: [10],
    route: [{ lat: 0, lon: 0 }, { lat: 0.001, lon: 0 }, { lat: 0, lon: 0 }],
  };
  const tl = m.cbbaTimeline(plan, { p1: { inspect_duration_s: 5 } }, 8);
  assert.equal(tl.legs.length, 2);
  assert.equal(tl.legs[1].t0, 15); // arrives at 10 s, hovers 5 s
  assert.ok(Math.abs(tl.end - (15 + 111.2 / 8)) < 0.2);
  assert.deepEqual(m.cbbaPositionAt(tl, 5), { lat: 0.0005, lon: 0 });
  assert.deepEqual(m.cbbaPositionAt(tl, 12), { lat: 0.001, lon: 0 }); // hovering at p1
  assert.deepEqual(m.cbbaPositionAt(tl, 1e6), { lat: 0, lon: 0 }); // home
});
