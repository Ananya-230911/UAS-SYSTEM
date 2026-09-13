# services/api

REST + WebSocket backend: persists telemetry, dispatches commands to each
vehicle's gateway, streams live telemetry to UI clients (per-vehicle and
fleet-wide).

## Authentication (Phase 5)

Every endpoint below except `GET /health` and `POST /internal/telemetry`
requires an API key **once `API_KEY` is set** (see
`docs/adr/0011-api-authentication.md`). Unset by default, in which case
none of this applies and every endpoint behaves exactly as in Phase 1-4:

```bash
export API_KEY=some-shared-secret
```

- REST: send it as `X-API-Key: some-shared-secret`
- WebSocket (`/ws/telemetry/{id}`, `/ws/fleet`): browsers can't set
  custom headers on a WS handshake, so send it as a query param instead:
  `ws://.../ws/fleet?api_key=some-shared-secret`

A request/connection missing the key, or with the wrong one, gets a `401`
(REST) or the socket is closed with code `1008` (WebSocket).

## Endpoints
- `GET /health` — no auth required
- `POST /internal/telemetry` — gateway → API telemetry ingest (requires
  `X-Internal-Token` header matching `UAS_INTERNAL_TOKEN`, a separate
  mechanism from `API_KEY` above)
- `GET /vehicles`, `GET /vehicles/{id}`
- `GET /vehicles/{id}/telemetry?limit=100` — recent history, oldest first
- `POST /vehicles/{id}/commands` — `{"type": "ARM"|"DISARM"|"TAKEOFF"|"RTL"|"MISSION_START", "altitude_m": ...}`,
  forwards to `{id}`'s own gateway (see `GATEWAY_URLS_JSON` below) and
  blocks for the result
- `GET /vehicles/{id}/commands/{command_id}`
- `POST /vehicles/{id}/missions` — `{"waypoints": [{"lat":, "lon":, "alt_m":}, ...]}`,
  uploads the mission to the vehicle via its gateway (Phase 2, see
  `docs/adr/0008-mission-protocol.md`)
- `GET /vehicles/{id}/missions`, `GET /vehicles/{id}/missions/{mission_id}`
- `POST /vehicles/{id}/missions/{mission_id}/start` — sends `MISSION_START`;
  mission must be `UPLOADED` first
- `POST /vehicles/{id}/geofence` — `{"points": [{"lat":, "lon":}, ...]}`
  (at least 3), replaces the vehicle's fence (Phase 6a, see
  `docs/adr/0012-geofencing-failsafe.md`)
- `GET /vehicles/{id}/geofence`, `DELETE /vehicles/{id}/geofence`
- `WS /ws/telemetry/{id}` — one vehicle's live telemetry stream (JSON
  messages, including `current_waypoint_seq` and `emergency_state`)
- `WS /ws/fleet` — every vehicle's live telemetry over one connection
  (Phase 3, see `docs/adr/0009-multi-vehicle-fleet.md`)

## Geofencing + failsafe (Phase 6a)

Once a vehicle has a geofence (`POST /vehicles/{id}/geofence`):
- `POST /vehicles/{id}/missions` rejects (`422`) any waypoint outside it.
- The failsafe manager watches every incoming telemetry sample
  (`POST /internal/telemetry`); if the vehicle is armed and its position
  is outside the fence, it auto-issues `RTL` and sets
  `Vehicle.emergency_state = "GEOFENCE_BREACH"` (visible on
  `GET /vehicles/{id}` and in every telemetry WS message). This resets
  to `"NORMAL"` once the vehicle disarms. See
  `docs/adr/0012-geofencing-failsafe.md` for the full design and its
  known limitations (one fence per vehicle, RTL is the only failsafe
  action, the check runs on this software stack rather than onboard).

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Storage is SQLite by default (`./uas.db`) — see
`docs/adr/0004-telemetry-storage.md` for why, and what changes to move to
TimescaleDB/Postgres. WAL mode + a busy timeout are enabled automatically
(`app/db.py`) so concurrent writes from a multi-vehicle fleet's gateways
don't hit "database is locked."

### Routing commands to the right vehicle's gateway

`GATEWAY_BASE_URL` is the default/fallback gateway (correct for a single
vehicle). For more than one, set `GATEWAY_URLS_JSON` to a JSON map from
`vehicle_id` to that vehicle's own gateway base URL, e.g.:

```bash
export GATEWAY_URLS_JSON='{"sim-2": "http://127.0.0.1:8002"}'
```

A `vehicle_id` missing from the map falls back to `GATEWAY_BASE_URL`.

## Tests

```bash
PYTHONPATH=. pytest tests/
```
