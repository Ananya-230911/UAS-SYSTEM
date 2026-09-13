# ADR-0001: Autopilot / Simulator

## Status
Accepted (with a Phase 1 substitution — see below)

## Context
Phase 1 needs a simulated vehicle to develop and test the telemetry/command
pipeline against, without real flight hardware.

## Decision
Target **PX4 SITL** speaking **MAVLink v2** as the long-term simulator of
record (widest ecosystem, first-class MAVSDK support, a clear path to
Gazebo-based physics in a later phase).

**Phase 1 substitution:** the development/CI sandbox this project is built in
does not have the several-GB PX4 build toolchain (Gazebo, ROS, firmware
source tree) or a working Docker daemon available. Building and running real
PX4 SITL is therefore deferred. In its place, `simulation/sitl/sim.py` is a
small hand-written Python service that speaks real MAVLink v2 over UDP
(HEARTBEAT, GLOBAL_POSITION_INT, ATTITUDE, SYS_STATUS, GPS_RAW_INT,
COMMAND_LONG/COMMAND_ACK) with a simple arm → takeoff → loiter flight model.

## Consequences
- Everything downstream (telemetry gateway, API, UI) only depends on the
  MAVLink wire protocol, not on which vehicle produced it. Swapping the
  stand-in simulator for real PX4/ArduPilot SITL later requires no change to
  `services/telemetry-gateway`, `services/api`, or `apps/gcs-web` — only a
  new entry in `infra/docker-compose.yml`.
- The stand-in simulator does not implement full PX4 mode semantics
  (STABILIZE/OFFBOARD/etc.) — it uses a small closed set of application-level
  modes (`STANDBY`, `ARMED`, `TAKEOFF`, `LOITER`, `LANDING`) encoded in the
  HEARTBEAT `custom_mode` field. This is sufficient to exercise Phase 1's
  arm/takeoff/telemetry vertical slice but is **not** a substitute for real
  autopilot behavior validation.
- **Follow-up — done in Phase 4:** this claim was validated against a real,
  natively-built PX4 v1.14.0 SITL (jMAVSim), not just architecturally
  argued. The gateway connected and worked completely unmodified. See
  `docs/adr/0010-real-px4-sitl-validation.md` for the full result,
  including a real-autopilot behavior gap (arming/EKF2-convergence
  timing) this simplified simulator doesn't need to model.
