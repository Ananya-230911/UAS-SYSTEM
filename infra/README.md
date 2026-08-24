# infra

Full-stack local run via Docker Compose:

```bash
docker compose -f infra/docker-compose.yml up --build
```

Then open `http://localhost:8080/?api=http://localhost:8000&vehicle=sim-1`.

Brings up, in order: `api` → `gateway` (waits for the API's healthcheck) →
`sitl` (the Phase 1 simulator, see `docs/adr/0001-autopilot-simulator.md`) →
`gcs-web` (static UI served by nginx).

**No Docker daemon available?** Use `scripts/run_local.sh` instead — it runs
the same four components as plain local processes (see that script and
`scripts/README.md`).
