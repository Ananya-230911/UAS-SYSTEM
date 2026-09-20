# ADR-0013: AI-Assisted Risk Monitoring + Remote ID Simulator (Phase 6b)

## Status
Accepted

## Context
Phase 6a (ADR-0012) deliberately deferred two pieces of the original
Phase 6 brief because they're a different kind of feature from
geofencing/failsafe: AI-assisted risk monitoring (flagging *unusual*
telemetry patterns, not enforcing a hard boundary) and a Remote ID
broadcast simulator (a regulatory identity/location broadcast, not a
safety action at all). This ADR covers both, now that the geofence
failsafe they build alongside is proven working.

## Decision

### Risk monitoring: a heuristic engine, not an ML model or LLM call

"AI-assisted" here means a small set of hand-written threshold rules
over a vehicle's last few telemetry samples (`app/risk_monitor.py`),
run on every `POST /internal/telemetry` the same way the Phase 6a
failsafe already runs on every ingest — no separate poller, no new
process. It is **not** a trained model and **not** a call to an LLM:
- A real ML anomaly detector needs training data this project has none
  of (there's no corpus of real problematic flights, only a hand-rolled
  simulator — see ADR-0001).
- An LLM call per telemetry sample (arriving ~1/vehicle/second) would
  need network access and an API key this project doesn't otherwise
  depend on, add real latency into the ingest hot path, and produce
  non-deterministic output for what should be a fast, explainable
  safety-adjacent check — the wrong tool for "is the battery draining
  too fast."

Four signals, each independently flaggable: `RAPID_BATTERY_DRAIN`
(battery % dropping faster than a per-minute threshold),
`GPS_DEGRADED` (fix type or satellite count below a threshold),
`UNEXPECTED_ALTITUDE_DROP` (a sudden altitude loss while armed and
*not* already in RTL/LANDING — those modes are supposed to descend),
and `ABNORMAL_SPEED` (groundspeed past a sanity bound). Flags combine
into a `LOW`/`MEDIUM`/`HIGH` `risk_level` via a fixed weight-and-
threshold scheme, deliberately simple enough to reason about and test
exactly (`tests/test_risk_monitor.py`), unlike a black-box score would
be.

**Deliberately advisory-only.** Unlike ADR-0012's geofence failsafe,
`assess_risk()` never calls `_dispatch_gateway_command()` or issues any
command — it only updates `Vehicle.risk_level`/`risk_flags`, which ride
along on `GET /vehicles/{id}` and every telemetry WebSocket message for
a human operator to see and act on. This is a considered line, not an
oversight: a "soft" heuristic guessing at unusual patterns is the wrong
kind of thing to let autonomously fly a vehicle — that's what the hard,
narrowly-scoped geofence rule is for. Tested explicitly
(`test_risk_monitoring_never_dispatches_a_command`).

### Remote ID: broadcast *content*, not a broadcast

Real Remote ID (US 14 CFR Part 89, built on the ASTM F3411 standard) is
a **radio** broadcast (Bluetooth/WiFi) transmitted directly from the
aircraft, receivable by any nearby phone running the right app — it has
no HTTP component in the real world. This project has no radio layer
to transmit over, so `app/remote_id.py` builds a payload with the same
*fields* ASTM F3411's Basic ID + Location + System messages carry
(aircraft ID, position/altitude/speed/heading, an operator location,
emergency status), computed from telemetry this project already
stores, and exposes it as `GET /vehicles/{id}/remote_id`. That is
explicitly simulating the broadcast's *content*, not the broadcast
mechanism itself — nothing here is receivable by a real Remote ID app,
and the endpoint's `message_version: "sim-1.0"` deliberately doesn't
claim to be a real ASTM F3411 version string.

**Operator location is a stand-in.** ASTM F3411 broadcasts a genuine
ground-control-station position, separate from the aircraft's. This
project tracks no such position, so the vehicle's own earliest known
telemetry position (its launch point) is used instead — a documented
simplification, not a claim that the operator was standing there.

**Emergency status reuses Phase 6a's `emergency_state`.** ASTM F3411
defines exactly this kind of field (an in-flight emergency declaration)
in its System Message; rather than invent a second concept, a non-
`NORMAL` `emergency_state` maps directly to `status.emergency: true`.

## Consequences
- `apps/gcs-web` shows the selected vehicle's risk level/flags inline
  with its other telemetry (color-coded at MEDIUM/HIGH), and a
  Remote ID panel with a "Fetch broadcast" button showing the current
  payload — on-demand, not auto-polled every telemetry tick, to avoid
  adding REST-call volume proportional to telemetry frequency for a
  feature that's for occasional inspection, not continuous monitoring.
- **Known limitation — thresholds are demonstration constants.** The
  battery-drain rate, GPS thresholds, altitude-drop distance, and speed
  bound in `risk_monitor.py` are reasonable-looking numbers for this
  project's simulator, not values validated against real flight data.
  A **follow-up trigger, not built now**: tuning these (or making them
  configurable per vehicle/deployment) once this is used against real
  telemetry where false positive/negative rates can actually be
  measured.
- **Known limitation — no historical risk view.** Only the current
  `risk_level`/`risk_flags` are kept (on the `Vehicle` row); there's no
  timeline of past risk assessments the way `EventLogEntry` gives one
  for commands and geofence breaches. A **follow-up trigger, not built
  now**: log each risk-level *change* as its own `EventLogEntry` if an
  audit trail of risk history is ever needed, mirroring how
  `ARMED_CHANGED` is already logged only on transitions rather than on
  every sample.
- **Known limitation — Remote ID has no real transport.** Reiterating
  the Decision section: this cannot be received by a real Remote-ID-
  compliant app, has no BLE/WiFi broadcast layer, and doesn't implement
  ASTM F3411's actual wire encoding. It demonstrates the *data model*
  a real implementation would need to broadcast, which is the
  appropriate scope for a project whose simulator (ADR-0001) has no
  physical radio to broadcast from in the first place.
- Closes out the original Phase 6 brief (geofencing, failsafe manager,
  emergency state machine — ADR-0012; risk monitoring, Remote ID — this
  ADR) as a set of provable, independently-tested slices rather than
  one large, harder-to-verify change.
