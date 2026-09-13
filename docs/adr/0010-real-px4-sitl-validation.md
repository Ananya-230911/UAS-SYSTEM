# ADR-0010: Real PX4 SITL Validation (Phase 4)

## Status
Accepted — validation complete, no production code changed

## Context
ADR-0001 made a specific, falsifiable bet: that `simulation/sitl/sim.py`
could be swapped for a real autopilot's SITL "without touching the
gateway, API, or UI," because everything downstream depends only on the
MAVLink wire protocol. That was deferred in Phase 1 as "once Docker-in-CI
is available." Phase 4's job was to actually test that claim.

No Docker daemon was available in the environment this was built in
(confirmed again this phase), so this validated PX4 SITL via a **native
source build** instead — the same thing Docker-based PX4 images do
internally, just without the container.

## What was done
Built PX4-Autopilot v1.14.0 from source for the `jmavsim` SITL target
(headless, `HEADLESS=1 make px4_sitl jmavsim`) — real PX4 flight-stack
firmware, not a stand-in, running against jMAVSim's physics simulation.
Two build-environment fixes were needed, both pre-existing incompatibilities
between PX4 v1.14.0's code and this toolchain's newer GCC 13 (not related
to UAS-SYSTEM's own code, and not applied to this repo):
- `platforms/posix/src/px4/common/px4_daemon/pxh.cpp` needed an explicit
  `#include <cstdint>` (GCC 13 stopped transitively including it).
- `cmake/px4_add_common_flags.cmake` needed `-Wno-error=array-bounds` for
  GCC > 12, to downgrade a false-positive in `matrix/Matrix.hpp` (an
  inlined 1×1 matrix special case, flagged by GCC 13's more aggressive
  array-bounds analysis) from a hard build failure to a warning.

These patches live only in the ephemeral PX4-Autopilot build checkout
used for this validation, not in this repository — they're PX4/GCC
compatibility fixes, not UAS-SYSTEM changes.

With real PX4 + jMAVSim running, **this repo's actual, unmodified
`services/telemetry-gateway`** (no code changes, no config beyond the
normal `VEHICLE_ID`/`API_BASE_URL` env vars) was pointed at it, because
PX4's default MAVLink stream already targets UDP port 14550 — the exact
port the gateway already listens on with `simulation/sitl/sim.py`.

## Result: the ADR-0001 bet paid off

- The gateway's `mavlink_connected` health check went `true` against real
  PX4's HEARTBEAT — the same code path that talks to `sim.py`.
- Real, physically-simulated telemetry flowed through the unmodified
  gateway → API → SQLite stack: GPS `47.3977419, 8.5455939` (PX4's actual
  default SITL home, Zurich — nothing to do with our simulator's Stanford
  default), realistic altitude/attitude/battery-voltage values, `gps_fix_type: 3`
  with 10 satellites.
- `POST /command {"type": "ARM"}` and `{"type": "TAKEOFF"}` against real
  PX4 both returned `{"mav_result": 0, "acked": true}` (`MAV_RESULT_ACCEPTED`)
  — the same `MAV_CMD_COMPONENT_ARM_DISARM` / `MAV_CMD_NAV_TAKEOFF` IDs
  `gateway/commands.py` already builds for the custom simulator.
- The gateway's `flight_mode` fallback (`MODE_NAMES.get(x, f"MODE_{x}")`
  in `gateway/mavlink_client.py`) correctly handled PX4's real
  `custom_mode` encoding (a large packed integer, nothing like our
  simulator's 0–6 scheme) by falling through to `MODE_{n}` instead of
  crashing or misreporting — exactly the defensive behavior it was
  written for, exercised against real data for the first time.

**No changes were needed to `services/telemetry-gateway`,
`services/api`, or `apps/gcs-web` to achieve any of this.** That is the
actual Phase 4 deliverable: confirmation, not new code.

## Known gap: real PX4 disarms before sustained flight

The armed vehicle disarmed itself shortly after each ARM/TAKEOFF pair
before altitude climbed, without a repeated preflight-check warning
(`health_and_arming_checks` had already logged `Preflight Fail: ekf2
missing data` / `GPS fix too low` once at simulator startup, before EKF2
had converged). This is **real autopilot safety behavior our simplified
`sim.py` doesn't model at all** — PX4 requires EKF2 convergence, adequate
GPS fix quality, and (unconfirmed here) likely a continued setpoint/
heartbeat stream to sustain a commanded takeoff, none of which a from-
scratch simulator needs to enforce. This is not a protocol-compatibility
problem — commands were accepted every time — it's a flight-behavior
tuning gap.

**Follow-up (not done here, out of scope for "prove the protocol
works"):** achieving sustained autonomous flight against real PX4 would
need either a longer warm-up before commanding ARM, or investigating
PX4's `COM_*` arming/disarm-timer parameters for a SITL-appropriate
configuration. This is real autopilot-specific tuning work, independent
of anything in this repo.

## Consequences
- ADR-0001's central claim is now empirically validated, not just
  architecturally argued.
- The custom `simulation/sitl/sim.py` remains the default for Phase 1-3
  development (fast, no toolchain, no GCC-version landmines) — this
  validation doesn't change that; it confirms the exit is still open.
- Real PX4 SITL was **not** made part of this repo's CI or local dev
  stack: it requires a multi-gigabyte native Linux build toolchain (or
  Docker, unavailable here) that doesn't fit this project's "run three
  Python processes" Phase 1 design goal, and doesn't run natively on
  Windows at all (the primary dev/test platform used for Phases 1-3 of
  this project) without WSL2. Adopting it as a routine dev dependency is
  a separate decision for whoever maintains this project long-term, not
  something this validation pass should force.
