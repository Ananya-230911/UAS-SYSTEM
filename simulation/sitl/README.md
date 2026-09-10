# simulation/sitl

Phase 1/2 MAVLink simulator stand-in. See
[`docs/adr/0001-autopilot-simulator.md`](../../docs/adr/0001-autopilot-simulator.md)
for why this exists instead of real PX4/ArduPilot SITL, and what it would
take to swap it in later.

## Run locally

```bash
pip install -r requirements.txt
python3 sim.py --target-host 127.0.0.1 --target-port 14550
```

Sends MAVLink v2 to `--target-host:--target-port` (the telemetry gateway's
MAVLink bind address). Streams HEARTBEAT (1 Hz) + position/attitude/
battery/GPS telemetry (5 Hz), plus `MISSION_CURRENT` while a mission is
active. Reacts to:
- `MAV_CMD_COMPONENT_ARM_DISARM`, `MAV_CMD_NAV_TAKEOFF` (Phase 1)
- The MAVLink mission upload handshake (`MISSION_COUNT`/`MISSION_REQUEST_INT`/
  `MISSION_ITEM_INT`/`MISSION_ACK`), `MAV_CMD_MISSION_START`, and
  `MAV_CMD_NAV_RETURN_TO_LAUNCH` (Phase 2, see
  [`docs/adr/0008-mission-protocol.md`](../../docs/adr/0008-mission-protocol.md))
