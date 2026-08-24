# ADR-0002: Telemetry Protocol

## Status
Accepted

## Context
Need a wire protocol between the vehicle (simulated now, real later) and the
ground system for telemetry and commands.

## Decision
**MAVLink v2**, the de facto standard for UAS telemetry/command, supported
natively by both PX4 and ArduPilot, with mature Python tooling (`pymavlink`).

## Consequences
- `services/telemetry-gateway` is the only service that speaks MAVLink;
  everything past it (API, storage, UI) deals only in normalized JSON. This
  keeps a future protocol change (or supporting a second protocol) contained
  to one service.
- Command semantics (arm/disarm via `MAV_CMD_COMPONENT_ARM_DISARM`, takeoff
  via `MAV_CMD_NAV_TAKEOFF`) follow the standard MAVLink command/ack pattern,
  so real autopilots can be dropped in without changing the command API.
