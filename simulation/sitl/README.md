# simulation/sitl

Phase 1 MAVLink simulator stand-in. See
[`docs/adr/0001-autopilot-simulator.md`](../../docs/adr/0001-autopilot-simulator.md)
for why this exists instead of real PX4/ArduPilot SITL, and what it would
take to swap it in later.

## Run locally

```bash
pip install -r requirements.txt
python3 sim.py --target-host 127.0.0.1 --target-port 14550
```

Sends MAVLink v2 to `--target-host:--target-port` (the telemetry gateway's
MAVLink bind address). Reacts to `MAV_CMD_COMPONENT_ARM_DISARM` and
`MAV_CMD_NAV_TAKEOFF`, and streams HEARTBEAT (1 Hz) + position/attitude/
battery/GPS telemetry (5 Hz).
