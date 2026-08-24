# UAS-SYSTEM

Unmanned Aerial System — ground control, telemetry, and mission management
software stack.

## Status: Phase 1 (foundation) implemented

See [`docs/PHASE_1_PLAN.md`](docs/PHASE_1_PLAN.md) for the full plan and
[`docs/adr/`](docs/adr/) for the architecture decisions behind it.

Phase 1 delivers a working vertical slice end-to-end against a simulated
vehicle: telemetry flows from a simulator through a MAVLink gateway into a
backend API and a live map UI, and arm/takeoff commands flow back the other
way.

```
simulation/sitl  --MAVLink/UDP-->  telemetry-gateway  --HTTP-->  api  <--WS/REST-->  gcs-web
   (simulator)                        (services/)              (services/)          (apps/)
```

## Quick start (no Docker required)

```bash
./scripts/run_local.sh
```

Then open `http://localhost:8080/?api=http://localhost:8000&vehicle=sim-1`
to see the simulated vehicle on the map, arm it, and command takeoff.

## Quick start (Docker)

```bash
docker compose -f infra/docker-compose.yml up --build
```

## Verify the vertical slice end-to-end

```bash
python3 -m venv .venv && .venv/bin/pip install \
  -r services/api/requirements.txt \
  -r services/telemetry-gateway/requirements.txt \
  -r simulation/sitl/requirements.txt
PYTHON=.venv/bin/python3 .venv/bin/python3 scripts/smoke_test.py
```

This is the same check CI runs on every push.

## Repository layout

| Path | Purpose |
|---|---|
| `docs/PHASE_1_PLAN.md` | Phase 1 plan |
| `docs/adr/` | Architecture decision records |
| `simulation/sitl/` | Phase 1 MAVLink simulator (stand-in for PX4/ArduPilot SITL — see ADR-0001) |
| `services/telemetry-gateway/` | MAVLink ↔ internal HTTP bridge |
| `services/api/` | REST + WebSocket backend, persistence |
| `apps/gcs-web/` | Minimal ground control station UI |
| `infra/` | Docker Compose for the full stack |
| `scripts/` | Local run + end-to-end smoke test |
| `.github/workflows/` | CI: lint, unit tests, integration smoke test |

Each service has its own `README.md` with endpoints/env vars/test commands.
