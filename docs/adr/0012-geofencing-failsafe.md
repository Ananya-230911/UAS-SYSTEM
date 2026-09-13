# ADR-0012: Geofencing + Failsafe Manager (Phase 6a)

## Status
Accepted

## Context
Phase 6's brief covers a broad set of "advanced/regulatory" capabilities:
geofencing, a deterministic failsafe manager, an emergency state machine,
AI-assisted risk monitoring, and a Remote ID simulator. Following the
same discipline every prior phase used (one provable slice at a time,
not five half-finished features), this ADR covers the first three, which
are one tightly-coupled unit — a fence with nothing enforcing it is just
a shape on a map, and enforcement needs somewhere to record "this vehicle
is currently in violation." AI-assisted risk monitoring and a Remote ID
simulator are real, separable features, deferred to Phase 6b.

## Decision

**Geofence shape.** A geofence is a polygon — a list of `{lat, lon}`
points, at least 3 — stored one per vehicle (`Geofence.vehicle_id` is
its primary key; posting a new one replaces the old one, the same
replace-not-append pattern Mission upload already uses). This reuses the
exact interaction Phase 2 already established for missions ("click the
map to add points") rather than inventing a second shape primitive
(e.g. circle+radius) with its own math and its own UI control.

**Point-in-polygon test.** Standard ray-casting over `(lon, lat)` as
plane coordinates (`app/geofence.py`). This ignores Earth's curvature,
which is the correct simplification at fence scale (meters to a few
kilometers) — the same reasoning that justifies an equirectangular
projection for a small area. A point exactly on the boundary is treated
as inside (fail-open on the line itself): the failsafe only needs to
catch clearly-outside, not litigate the millimeter at the edge.

**Mission upload validation.** `POST /vehicles/{id}/missions` now checks
every waypoint against the vehicle's fence (if it has one) *before*
creating the mission or contacting the gateway at all, rejecting with
`422` and the offending waypoint indices. Catching this at upload time
is strictly better than catching it later: no history of a
speculatively-created mission, and no letting a real drone fly toward a
forbidden point in the first place just to have the failsafe manager
turn it around after the fact.

**Failsafe manager.** A small, deterministic rule bolted onto the
existing `POST /internal/telemetry` handler (not a separate poller or
background task — telemetry already arrives ~1/second per vehicle, so
there's no missing-check window worth a second process for). The rule:
if the vehicle has a fence, is armed, and its latest position falls
outside it, issue `RTL` via the same `_dispatch_gateway_command()` every
other command already uses. Explicitly a fixed rule, not a learned or
scored system — "AI-assisted risk monitoring" (Phase 6b) is a different,
softer kind of check (e.g. flagging *unusual* telemetry patterns) and
deliberately isn't conflated with this hard safety trigger.

**Emergency state machine.** `Vehicle.emergency_state`: `NORMAL` or
`GEOFENCE_BREACH`. Two states only, on purpose:
- `NORMAL -> GEOFENCE_BREACH`: set the instant a breach is detected,
  *before* the RTL dispatch call, and logged as its own `EventLogEntry`
  (`GEOFENCE_BREACH`) separate from the `FAILSAFE_RTL` entry recording
  the dispatch outcome — so "a breach happened" and "did the RTL
  actually get acked" are each visible on their own, independently of
  whether the gateway call itself succeeded.
- Debounced: once `GEOFENCE_BREACH`, staying outside the fence on
  further telemetry samples does not re-issue RTL every ~1s. Tested
  explicitly (`test_geofence_breach_does_not_retrigger_rtl_on_every_subsequent_sample`).
- `GEOFENCE_BREACH -> NORMAL`: reset whenever a *disarmed* sample
  arrives for that vehicle, not by detecting "back inside the fence" —
  a disarmed vehicle can't be meaningfully outside anything, and this
  avoids having to reason about oscillation right at the boundary while
  still armed and possibly still moving.
- **Follow-up, not built now:** a fuller machine (e.g. a distinct
  `RTL_COMMANDED` state separate from `GEOFENCE_BREACH` itself, or a
  state for "RTL was attempted and failed") would earn its keep once a
  second failsafe trigger exists (e.g. low battery, GPS-fix loss) and
  states need to compose — premature with exactly one trigger.

**Auth.** All three new endpoints (`POST`/`GET`/`DELETE
/vehicles/{id}/geofence`) go through the same `require_api_key`
dependency as every other vehicle-facing route (ADR-0011) — a fence
defines what a vehicle is and isn't allowed to do, which is exactly the
kind of thing Phase 5's auth model exists to protect.

## Consequences
- `apps/gcs-web` gained a "draw fence" mode (reusing the map-click
  interaction) and shows the vehicle's emergency state alongside its
  other telemetry fields, plus the fence itself as an overlay.
- `emergency_state` rides along in every telemetry WebSocket message
  (`/ws/telemetry/{id}` and `/ws/fleet`) rather than requiring a
  separate poll — consistent with `armed`/`flight_mode` already being
  in that payload.
- **Known limitation — one fence per vehicle, not per-mission or
  per-zone.** No support for multiple exclusion zones, altitude
  ceilings/floors, or time-bounded fences (e.g. "no-fly only during a
  scheduled window"). A **follow-up trigger, not built now**: if a real
  deployment needs more than one boundary concept, this is where a
  `zones` table with a `kind` (inclusion/exclusion) would go.
- **Known limitation — RTL is the only failsafe action.** No "land in
  place," "hold position," or "return to a specific safe point other
  than home." Adequate for a first failsafe; a genuinely broader
  failsafe manager would parameterize the response.
- **Known limitation — the check runs on the API, not onboard.** Like
  every command in this system, the failsafe depends on telemetry
  reaching the API and a command reaching the gateway over the network;
  it is not a substitute for an autopilot's own onboard geofence (which
  real PX4 has natively — see ADR-0010) in a deployment where the link
  itself is what fails. This system's failsafe covers "this software
  stack detected a problem," not "the vehicle is safe even if this
  software stack disappears."
- Sets up Phase 6b (AI-assisted risk monitoring, Remote ID simulator) to
  build on a vehicle that already has a fence concept and an emergency
  state field to extend, rather than starting from nothing.
