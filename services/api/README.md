# services/api

REST + WebSocket backend: persists telemetry, dispatches commands to the
gateway, streams live telemetry to UI clients.

## Endpoints
- `GET /health`
- `POST /internal/telemetry` — gateway → API telemetry ingest (requires
  `X-Internal-Token` header matching `UAS_INTERNAL_TOKEN`)
- `GET /vehicles`, `GET /vehicles/{id}`
- `GET /vehicles/{id}/telemetry?limit=100` — recent history, oldest first
- `POST /vehicles/{id}/commands` — `{"type": "ARM"|"DISARM"|"TAKEOFF", "altitude_m": ...}`,
  forwards to the gateway and blocks for the result
- `GET /vehicles/{id}/commands/{command_id}`
- `WS /ws/telemetry/{id}` — live telemetry stream (JSON messages)

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
