# ADR-0008: Mission Upload Protocol

## Status
Accepted

## Context
Phase 2 needs a way to send a vehicle an ordered list of waypoints and have
it fly them autonomously, plus a return-to-launch command.

## Decision
Use the real, minimal MAVLink mission protocol rather than a custom one:

- Gateway → vehicle: `MISSION_COUNT(n)`, then for each requested item
  `MISSION_ITEM_INT(seq, frame=MAV_FRAME_GLOBAL_RELATIVE_ALT,
  command=MAV_CMD_NAV_WAYPOINT, x=lat*1e7, y=lon*1e7, z=alt_m)`.
- Vehicle → gateway: `MISSION_REQUEST_INT(seq)` for each item in order, then
  `MISSION_ACK(MAV_MISSION_ACCEPTED)` once all items are received.
- Starting the mission and returning to launch reuse the existing
  command/ack infrastructure from Phase 1 (`COMMAND_LONG`/`COMMAND_ACK`)
  with `MAV_CMD_MISSION_START` (300) and `MAV_CMD_NAV_RETURN_TO_LAUNCH` (20).
- Mission progress is reported via `MISSION_CURRENT(seq)`, folded into the
  existing telemetry sample as `current_waypoint_seq`.

Only the `_INT` message variants are implemented (not the legacy
non-`_INT` `MISSION_ITEM`/`MISSION_REQUEST`) since both PX4 and ArduPilot
support the `_INT` variants natively -- this keeps the door open for
ADR-0001's real-SITL swap without a protocol change.

## Consequences
- The simulator now has to speak both sides correctly (request items in
  order, ack, then fly them), which is real MAVLink mission behavior, not
  a simplified stand-in -- consistent with ADR-0001's "swap the simulator,
  not the protocol" principle.
- **Known Phase 2 simplification:** the API does not auto-detect mission
  completion (no `COMPLETED` status transition) -- it tracks `PENDING` →
  `UPLOADED` → `ACTIVE` (or `FAILED`), and callers observe progress via
  `current_waypoint_seq` in telemetry. Auto-completing missions robustly
  needs `MISSION_ITEM_REACHED`/`MISSION_CURRENT` history rather than a single
  snapshot field, which is Phase 3 fleet-dashboard-adjacent work, not core
  to proving the mission vertical slice.
- Waypoints are stored as a JSON column on `Mission` rather than a
  normalized `Waypoint` table -- adequate for the list-of-points-per-mission
  access pattern Phase 2 needs; revisit if per-waypoint querying
  (e.g. "which missions touch this geofence") becomes necessary.
