# services/api

REST + WebSocket backend: persists telemetry, dispatches commands to the
gateway, streams live telemetry to UI clients.

## Endpoints
- `GET /health`
- `POST /internal/telemetry` — gateway → API telemetry ingest (requires
  `X-Internal-Token` header matching `UAS_INTERNAL_TOKEN`)
- `GET /vehicles`, `GET /vehicles/{id}`
- `GET /vehicles/{id}/telemetry?limit=100` — recent history, oldest first
- `POST /vehicles/{id}/commands` — `{"type": "ARM"|"DISARM"|"TAKEOFF"|"RTL", "altitude_m": ...}`,
  forwards to the gateway and blocks for the result
- `GET /vehicles/{id}/commands/{command_id}`
- `POST /vehicles/{id}/missions` — `{"waypoints": [{"lat":, "lon":, "alt_m":}, ...]}`,
  uploads the mission to the vehicle via the gateway (Phase 2, see
  `docs/adr/0008-mission-protocol.md`)
- `GET /vehicles/{id}/missions`, `GET /vehicles/{id}/missions/{mission_id}`
- `POST /vehicles/{id}/missions/{mission_id}/start` — sends `MISSION_START`;
  mission must be `UPLOADED` first
- `WS /ws/telemetry/{id}` — live telemetry stream (JSON messages, now
  including `current_waypoint_seq`)

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Storage is SQLite by default (`./uas.db`) — see
`docs/adr/0004-telemetry-storage.md` for why, and what changes to move to
TimescaleDB/Postgres.

## Tests

```bash
PYTHONPATH=. pytest tests/
```
