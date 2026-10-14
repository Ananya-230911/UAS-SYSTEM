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

## CBBA multi-drone panel (simulation)

The **Multi-drone (simulation)** sections in the right-hand panel
(`cbba.js`): set up drones and places by clicking the map (anywhere), plan
with CBBA via `services/fleet-agent`'s read-only server, replay how the
drones decided round by round, and fly the plan in a browser-side
simulation (with a "make this drone fail" re-plan). It sends nothing to any
vehicle. Start the server with `scripts/run_cbba.ps1`; the
page defaults to `http://localhost:8020` (override with `?cbba=...`).

**★ Quick survey** (top of that panel): search any place, type *"3 drones
survey this area"*, press **Go** -- survey points over the middle of the
view, CBBA, safety check, flight playback, 📷 camera result per point and a
summary. Orange lines = drones exchanging bids. See
`docs/adr/0021-one-line-survey.md`.
