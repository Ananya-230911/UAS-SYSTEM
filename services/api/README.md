# services/api

REST + WebSocket backend: persists telemetry, dispatches commands to each
vehicle's gateway, streams live telemetry to UI clients (per-vehicle and
fleet-wide).

## Endpoints
- `GET /health`
- `POST /internal/telemetry` — gateway → API telemetry ingest (requires
  `X-Internal-Token` header matching `UAS_INTERNAL_TOKEN`)
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
- `WS /ws/telemetry/{id}` — one vehicle's live telemetry stream (JSON
  messages, including `current_waypoint_seq`)
- `WS /ws/fleet` — every vehicle's live telemetry over one connection
  (Phase 3, see `docs/adr/0009-multi-vehicle-fleet.md`)

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
