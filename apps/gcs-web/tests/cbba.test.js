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

test("playback: frame at a time, inspectors so far, plan in force", () => {
  const frames = [{ t: 0 }, { t: 1 }, { t: 2 }, { t: 3 }];
  assert.equal(m.cbbaFrameAt(frames, -5).t, 0);
  assert.equal(m.cbbaFrameAt(frames, 1.7).t, 1);
  assert.equal(m.cbbaFrameAt(frames, 99).t, 3);
  assert.equal(m.cbbaFrameAt([], 1), null);
  const events = [
    { t: 10, kind: "inspect", drone: "a", text: "a inspected P01-roof." },
    { t: 20, kind: "detect", drone: "b", text: "b has had no news of a" },
    { t: 30, kind: "inspect", drone: "b", text: "b inspected P01-roof (already done by a - double work)." },
    { t: 40, kind: "inspect", drone: "b", text: "b inspected P02." },
  ];
  assert.deepEqual(m.cbbaInspectorsUpTo(events, 35), { "P01-roof": "a" });
  assert.deepEqual(m.cbbaInspectorsUpTo(events, 100), { "P01-roof": "a", P02: "b" });
  const epochs = [{ epoch: 1, started_t: 0 }, { epoch: 2, started_t: 45 }];
  assert.equal(m.cbbaEpochAt(epochs, 44).epoch, 1);
  assert.equal(m.cbbaEpochAt(epochs, 45).epoch, 2);
  assert.equal(m.CBBA_STATUS_TEXT["lost-link"], "no radio contact");
});

test("an instruction's checked scenario updates only allowed fields in the editor", () => {
  const state = {
    drones: [D("a", 1, 1), D("b", 1, 1)],
    points: [P("p1", 2, 2), P("p2", 3, 3, { sensor: "thermal" })],
    fence: [{ lat: 0, lon: 0 }], radioMode: "range", rangeM: 50, missionId: "M-1",
  };
  const sc = {
    drones: [{ drone_id: "a", health: "OK" }, { drone_id: "b", health: "OFFLINE" }],
    points: [{ point_id: "p1", required_capability: "lidar", value: 150, deadline_s: 60, lat: 99, lon: 99 }],
  };
  const out = m.cbbaApplyScenario(state, sc);
  assert.deepEqual(out.points.map((p) => p.id), ["p1"]); // p2 skipped
  assert.equal(out.points[0].sensor, "lidar");
  assert.equal(out.points[0].value, 150);
  assert.equal(out.points[0].deadline, 60);
  assert.equal(out.points[0].lat, 2); // positions never come from the server
  assert.equal(out.drones[1].offline, true);
  assert.equal(out.radioMode, "range");
  assert.equal(out.missionId, "M-1");
});

// ---- quick survey (Phase F) ----
const { cbbaCountsText, cbbaSumCounts, cbbaTalkAt, cbbaInspections, cbbaMapView } = require("../cbba.js");

test("camera counts read as plain English, largest first", () => {
  assert.equal(cbbaCountsText({ car: 2, person: 4, dog: 1 }), "4 people, 2 cars, 1 dog");
  assert.equal(cbbaCountsText({ person: 1, bus: 3 }), "3 buses, 1 person");
  assert.equal(cbbaCountsText({}), "nothing detected");
  assert.deepEqual(cbbaSumCounts([{ car: 1 }, { car: 2, person: 1 }, null]), { car: 3, person: 1 });
});

test("talk lines: bid messages from the last 2 s, each pair once", () => {
  const frames = [
    { t: 0, talk: [] },
    { t: 1, talk: [["D1", "D2"], ["D2", "D1"]] },
    { t: 2, talk: [["D2", "D3"]] },
    { t: 3, talk: [] },
    { t: 9, talk: [] },
  ];
  assert.deepEqual(cbbaTalkAt(frames, 2), [["D1", "D2"], ["D2", "D3"]]);
  assert.deepEqual(cbbaTalkAt(frames, 3), [["D2", "D3"]]);
  assert.deepEqual(cbbaTalkAt(frames, 9), []);
  assert.deepEqual(cbbaTalkAt([{ t: 1 }], 1), []); // older servers send no talk
});

test("first inspection of each point, in time order", () => {
  const events = [
    { t: 5, kind: "plan", drone: null, text: "Plan agreed." },
    { t: 40, kind: "inspect", drone: "D2", text: "D2 inspected S04." },
    { t: 55, kind: "inspect", drone: "D1", text: "D1 inspected S01 (hovered 0 s)." },
    { t: 60, kind: "inspect", drone: "D3", text: "D3 inspected S04." },
  ];
  assert.deepEqual(cbbaInspections(events), [
    { point: "S04", drone: "D2", t: 40 },
    { point: "S01", drone: "D1", t: 55 },
  ]);
});

test("map view is kept inside valid coordinates", () => {
  assert.deepEqual(cbbaMapView(-95, -200, 95, 190), { south: -89.9, west: -180, north: 89.9, east: 180 });
});

// ---- story box ----
const { cbbaStepStory, cbbaReplaySteps } = require("../cbba.js");

const round1 = {
  round: 1, links: [["D1", "D2"], ["D1", "D3"], ["D2", "D3"]], messages: 6,
  wants: { D1: ["S01", "S02"], D2: ["S02", "S01"], D3: ["S04"] },
  claims: { D1: ["S01"], D2: ["S02"], D3: ["S04"] },
  events: [
    { kind: "bid", drone: "D1", point: "S01", bid: 97.63 },
    { kind: "bid", drone: "D1", point: "S02", bid: 94.03 },
    { kind: "bid", drone: "D2", point: "S02", bid: 97.06 },
    { kind: "bid", drone: "D2", point: "S01", bid: 90.05 },
    { kind: "bid", drone: "D3", point: "S04", bid: 97.63 },
    { kind: "dropped", drone: "D1", point: "S02", bid: 94.03, reason: "outbid", winner: "D2", winner_bid: 97.06 },
    { kind: "dropped", drone: "D2", point: "S01", bid: 90.05, reason: "outbid", winner: "D1", winner_bid: 97.63 },
    { kind: "dropped", drone: "D3", point: "S09", bid: null, reason: "after_lost" },
  ],
};

test("story: thinking alone lists what each drone wants, with bids and conflicts", () => {
  const [bid] = cbbaReplaySteps([round1]);
  const st = cbbaStepStory(bid);
  assert.equal(st.title, "Round 1 · 🧠 Thinking alone");
  assert.deepEqual(st.lines.map((l) => l.text), [
    "D1 wants 2 place(s): S01 (bid 98), S02 (bid 94)",
    "D2 wants 2 place(s): S02 (bid 97), S01 (bid 90)",
    "D3 wants 1 place(s): S04 (bid 98)",
  ]);
  assert.match(st.intro, /2 place\(s\) are wanted by more than one drone/);
});

test("story: talking says who won each place and who gave it up", () => {
  const talk = cbbaReplaySteps([round1])[1];
  const st = cbbaStepStory(talk);
  assert.equal(st.title, "Round 1 · 📡 Talking");
  assert.match(st.intro, /6 message\(s\) over 3 radio link\(s\)/);
  assert.deepEqual(st.lines.map((l) => l.text), [
    "S02 → D2 (bid 97); D1 (94) gives it up",
    "S01 → D1 (bid 98); D2 (90) gives it up",
    "D3 also lets go of S09 (they came after a place it lost)",
  ]);
  assert.equal(st.kept, "D1: S01 · D2: S02 · D3: S04");
});

test("story: a quiet round says nobody had to give anything up; long lists are cut", () => {
  const quiet = { ...round1, events: [] };
  assert.equal(cbbaStepStory(cbbaReplaySteps([quiet])[1]).lines[0].text, "No conflicts: nobody had to give anything up.");
  const many = { ...round1, events: Array.from({ length: 9 }, (_, i) => ({ kind: "dropped", drone: "D1", point: `S${10 + i}`, bid: 50, reason: "outbid", winner: "D2", winner_bid: 60 })) };
  const lines = cbbaStepStory(cbbaReplaySteps([many])[1], 6).lines;
  assert.equal(lines.length, 7);
  assert.equal(lines[6].text, "+ 3 more");
});
