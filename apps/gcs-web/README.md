# apps/gcs-web

Minimal ground control station UI: live map (Leaflet), telemetry readout,
arm/disarm/takeoff controls. No build step — see
[`docs/adr/0005-frontend-stack.md`](../../docs/adr/0005-frontend-stack.md).

## Run locally

Any static file server works, e.g.:

```bash
python3 -m http.server 8080
```

Then open `http://localhost:8080/?api=http://localhost:8000&vehicle=sim-1`.

`api` and `vehicle` query params default to `http://localhost:8000` and
`sim-1` respectively, matching the local dev stack in
`scripts/run_local.sh`.
