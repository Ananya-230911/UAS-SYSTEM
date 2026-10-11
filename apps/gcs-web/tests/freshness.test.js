const test = require("node:test");
const assert = require("node:assert/strict");
const { freshnessOf, STALE_THRESHOLD_MS, DISCONNECTED_THRESHOLD_MS } = require("../freshness.js");

test("no timestamp at all is unknown, never faked as live", () => {
  assert.equal(freshnessOf(null), "unknown");
  assert.equal(freshnessOf({}), "unknown");
});

test("a very recent sample is live", () => {
  const now = Date.now();
  assert.equal(freshnessOf({ timestamp: new Date(now).toISOString() }, now), "live");
});

test("right at the stale threshold is still live", () => {
  const now = Date.now();
  const ts = new Date(now - STALE_THRESHOLD_MS).toISOString();
  assert.equal(freshnessOf({ timestamp: ts }, now), "live");
});

test("just past the stale threshold is stale, not live", () => {
  const now = Date.now();
  const ts = new Date(now - STALE_THRESHOLD_MS - 1).toISOString();
  assert.equal(freshnessOf({ timestamp: ts }, now), "stale");
});

test("just past the disconnected threshold is disconnected, not stale", () => {
  const now = Date.now();
  const ts = new Date(now - DISCONNECTED_THRESHOLD_MS - 1).toISOString();
  assert.equal(freshnessOf({ timestamp: ts }, now), "disconnected");
});

test("last_seen is used when timestamp is absent (a vehicle seeded via GET /vehicles)", () => {
  const now = Date.now();
  assert.equal(freshnessOf({ last_seen: new Date(now).toISOString() }, now), "live");
});

test("timestamp is preferred over last_seen when both are present", () => {
  const now = Date.now();
  const stalePast = new Date(now - DISCONNECTED_THRESHOLD_MS - 1).toISOString();
  const sample = { timestamp: new Date(now).toISOString(), last_seen: stalePast };
  assert.equal(freshnessOf(sample, now), "live");
});
