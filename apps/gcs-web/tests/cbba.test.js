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
