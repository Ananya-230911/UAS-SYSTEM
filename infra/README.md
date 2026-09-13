# infra

Full-stack local run via Docker Compose:

```bash
docker compose -f infra/docker-compose.yml up --build
```

Then open `http://localhost:8080/?api=http://localhost:8000` (no `vehicle=`
needed as of Phase 3 -- the UI discovers every vehicle and shows a fleet
list, see `docs/adr/0009-multi-vehicle-fleet.md`).

Brings up, in order: `api` → `gateway`/`gateway-2` (each waits for the
API's healthcheck) → `sitl`/`sitl-2` (the Phase 1 simulator, see
`docs/adr/0001-autopilot-simulator.md`) → `gcs-web` (static UI served by
nginx). Two vehicles (`sim-1`, `sim-2`) come up by default; add another
`gateway-N`/`sitl-N` pair the same way for a third.

**No Docker daemon available?** Use `scripts/run_local.sh` instead — it runs
the same components as plain local processes (see that script and
`scripts/README.md`).
