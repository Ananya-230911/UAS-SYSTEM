# services/telemetry-gateway

Bridges MAVLink (talking to `simulation/sitl` or, later, a real
autopilot/SITL) to the internal HTTP contract the API service consumes.
See `docs/adr/0002-telemetry-protocol.md` and `docs/adr/0006-internal-messaging.md`.

## Responsibilities
- Binds a MAVLink UDP endpoint and receives HEARTBEAT/GLOBAL_POSITION_INT/
  ATTITUDE/SYS_STATUS/GPS_RAW_INT/MISSION_CURRENT from the vehicle.
- Normalizes the latest state into a flat sample and `POST`s it to the API's
  `POST /internal/telemetry` on a fixed interval (only when something changed).
- Exposes `POST /command` (`{"type": "ARM"|"DISARM"|"TAKEOFF"|"RTL"|"MISSION_START", "altitude_m": ...}`)
  which the API calls; translates it to a MAVLink `COMMAND_LONG` and blocks
  for the `COMMAND_ACK`.
- Exposes `POST /mission` (`{"waypoints": [{"lat":, "lon":, "alt_m":}, ...]}`)
  which the API calls to upload a mission; runs the full MAVLink mission
  handshake (`MISSION_COUNT` → `MISSION_REQUEST_INT`/`MISSION_ITEM_INT` →
  `MISSION_ACK`) and blocks for the result. See
  `docs/adr/0008-mission-protocol.md`.

## Run locally

```bash
pip install -r requirements.txt
uvicorn gateway.main:app --host 0.0.0.0 --port 8001
```

Env vars (defaults shown): `VEHICLE_ID=sim-1`, `GATEWAY_MAVLINK_HOST=0.0.0.0`,
`GATEWAY_MAVLINK_PORT=14550`, `API_BASE_URL=http://127.0.0.1:8000`,
`UAS_INTERNAL_TOKEN=dev-secret`, `TELEMETRY_POST_INTERVAL_S=0.5`.

## Tests

```bash
PYTHONPATH=. pytest tests/
```
