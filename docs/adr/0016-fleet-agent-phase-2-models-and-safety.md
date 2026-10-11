# ADR-0016: Fleet-Agent Phase 2 — Data Models + Safety Layer

## Status
Accepted

## Context
`docs/IMPLEMENTATION_PLAN.md` asks for a much larger transformation
than any prior phase of this project: cooperative multi-drone agents,
mesh comms, task auctioning, object detection, and a human-in-the-loop
GCS. Phase 0 of that plan (a full repository audit) found this
project's existing fleet-routing architecture (ADR-0009) already
covers much of what the plan's own Phase 1 asked for, and recommended
building the new work as its own separate service rather than inside
`services/api` — the same reasoning ADR-0015 already used for
`services/perception`.

This ADR covers only Phase 2 of that plan: "one shared language and a
strict safety checker," the deterministic foundation every later
phase (planning, agents, comms, execution) will build on. Following
this project's established discipline (one provable slice at a time —
see ADR-0012/ADR-0013's identical split of a single larger brief),
Phase 2 ships as its own ADR, not folded into a future catch-all.

## Decision

**A new, separate service: `services/fleet-agent/`.** No HTTP server
yet, no dependency on `services/api`'s database, no LLM, no mesh comms
— purely a typed data model layer (`app/core/models.py`) and a
deterministic safety validator (`app/safety/`), both directly unit
tested without any running process.

**Models are Pydantic, independent of `services/api`'s own schemas.**
`TelemetrySnapshot` deliberately duplicates the relevant subset of
`services/api/app/schemas.py`'s `TelemetryOut` rather than importing
it — services in this project stay independent on purpose
(ADR-0006), the same reason `gateway/commands.py`'s `MODE_NAMES` and
`sim.py`'s `MODE_CODES` are two separate tables instead of one shared
module.

**Geofence geometry is duplicated, not imported, for the same reason.**
`app/safety/geofence.py`'s `point_in_polygon()` is byte-for-byte the
same ray-casting algorithm as `services/api/app/geofence.py`'s
function of the same name. New in this module:
`segment_crosses_fence_boundary()` — `services/api` only ever checks
whether individual *waypoints* are inside a fence; this plan's Phase 2
explicitly also requires checking whether the straight line *between*
two waypoints clips outside a concave fence, which point-in-polygon
alone cannot catch.

**Safety checks implemented, each independently testable:**
- Altitude within a configurable `[min_altitude_m, max_altitude_m]`.
- Every waypoint, and every route segment, inside the geofence.
- Launch/home point inside the geofence.
- An endurance estimate (flight time for the route *plus* return
  home, at a configurable cruise speed and battery-drain rate) that
  must leave at least a configured reserve. `cruise_speed_ms` and
  `battery_drain_pct_per_min` default to `simulation/sitl/sim.py`'s
  own constants (`WAYPOINT_SPEED_MS=8.0`, its 0.01%/second armed
  drain rate), so the estimate is at least consistent with what the
  simulator will actually do, not an arbitrary guess.
- Inter-vehicle separation: each vehicle gets its own altitude band
  (`assign_altitude_bands()` — simple, deterministic, not an
  optimizer); two vehicles not already vertically deconflicted by
  `altitude_band_spacing_m` must keep `min_horizontal_separation_m`
  apart at every corresponding waypoint.

**Plan tampering after approval is caught by content hashing, not a
status flag.** `MissionPlan.content_hash()` hashes everything about a
plan's content (tasks, assignments, the originating request)
*excluding* `plan_id` itself — `plan_id` is identity, not content,
confirmed by `test_content_hash_ignores_plan_id`. `ApprovalRecord`
stores that hash at approval time; `plan_matches_approval()`
recomputes it and requires both the `plan_id` *and* the hash to match.
This is the mechanism Phase 5 (not built yet) will call immediately
before dispatching anything to a vehicle.

## Consequences
- 30 new tests (`test_models.py`, `test_safety.py`), all passing —
  covering the exact list Phase 2 of the plan asked for: inside/
  outside/boundary points, a segment crossing a concave fence, bad
  altitude, a separation conflict, and a plan modified after approval.
- **Known limitation — the separation check assumes routes progress in
  lockstep.** Using "same list index" as a stand-in for "same time" is
  documented in `check_separation()`'s own docstring, not silently
  assumed. A real time-synced check needs per-waypoint ETAs, which
  Phase 3's coverage/allocation (not built yet) doesn't compute.
- **Known limitation — thresholds are demonstration constants**, the
  same caveat `services/api/app/risk_monitor.py` and
  `services/perception`'s decision layer already carry.
- **Nothing here is wired to a live vehicle, gateway, or the existing
  `services/api`.** This phase is intentionally pure, testable logic
  only — `services/api`'s own command/mission endpoints, and every
  safety property they already enforce (ADR-0011's auth, ADR-0012's
  geofence rejection), are completely untouched. Integration (an
  executor that calls `services/api`'s existing endpoints with an
  approved, re-validated plan) is Phase 5 of the plan, not this ADR.
