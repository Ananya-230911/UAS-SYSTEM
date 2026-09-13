# apps/gcs-web

Minimal ground control station UI: fleet map + list (Leaflet), telemetry
readout, arm/disarm/takeoff/RTL controls, and mission planning. No build
step — see
[`docs/adr/0005-frontend-stack.md`](../../docs/adr/0005-frontend-stack.md).

## Run locally

Any static file server works, e.g.:

```bash
python3 -m http.server 8080
```

Then open `http://localhost:8080/?api=http://localhost:8000&vehicle=sim-1`.

`api` defaults to `http://localhost:8000`. `vehicle` is optional as of
Phase 3 — the page discovers every vehicle that has ever sent telemetry
via `GET /vehicles`, and if `vehicle` isn't given it just focuses whichever
vehicle it hears from first (see
[`docs/adr/0009-multi-vehicle-fleet.md`](../../docs/adr/0009-multi-vehicle-fleet.md)).
Click any vehicle in the **Fleet** list, or its marker on the map, to
switch which one the telemetry panel, commands, and mission planner apply
to.
