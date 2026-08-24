# ADR-0005: Frontend Stack

## Status
Accepted (with a Phase 1 substitution)

## Context
Need a minimal GCS web UI: live map with vehicle position, a telemetry
readout, and arm/disarm/takeoff controls.

## Decision
Target **React + TypeScript + MapLibre GL** for the long-term GCS frontend,
as originally sketched in the Phase 1 plan.

**Phase 1 substitution:** `apps/gcs-web` is a small **vanilla HTML/CSS/JS**
page using **Leaflet** (via CDN) for the map, talking to the API over a
native `WebSocket` and `fetch`. No build step, no npm dependency tree to
install/audit for what is, functionally, one map, one status panel, and
three buttons. This removes an entire toolchain (bundler, dev server,
Node dependency resolution) from Phase 1's "does the vertical slice work"
question, so the exit criterion (live telemetry, arm/takeoff working) can be
verified without also debugging a frontend build.

## Consequences
- The UI is intentionally minimal and not componentized — fine for one
  vehicle and three controls, not fine for a fleet dashboard.
- **Follow-up (Phase 2 backlog item):** migrate `apps/gcs-web` to React +
  TypeScript once the UI needs more than a single map + status panel
  (multi-vehicle views, mission editor, reusable component library). The
  WebSocket/REST contract this page consumes is already the real API
  contract, so the migration is a frontend rewrite against a stable backend,
  not a redesign of both at once.
