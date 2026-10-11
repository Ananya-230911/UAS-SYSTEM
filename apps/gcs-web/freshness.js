// Freshness classification for fleet telemetry (stale/disconnected).
//
// A vehicle's own telemetry can stop arriving (its simulator or
// gateway died) even while the fleet WebSocket connection itself stays
// open -- "live" at the socket level isn't the same as "this vehicle
// is still reporting in". Pure, DOM-free logic (no `document`/`L`
// globals) so it's testable with plain Node -- see tests/freshness.test.js.
//
// Loaded as a plain <script> before app.js (see index.html), which
// makes these ordinary globals in the browser (this project has no
// build step -- see docs/adr/0005-frontend-stack.md); the same file is
// also require()-able from Node for tests.

const STALE_THRESHOLD_MS = 3000;
const DISCONNECTED_THRESHOLD_MS = 15000;

function freshnessOf(sample, nowMs = Date.now()) {
  const lastSeen = sample && (sample.timestamp || sample.last_seen);
  if (!lastSeen) return "unknown";
  const ageMs = nowMs - new Date(lastSeen).getTime();
  if (ageMs > DISCONNECTED_THRESHOLD_MS) return "disconnected";
  if (ageMs > STALE_THRESHOLD_MS) return "stale";
  return "live";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { freshnessOf, STALE_THRESHOLD_MS, DISCONNECTED_THRESHOLD_MS };
}
