# UAS-SYSTEM

Unmanned Aerial System — ground control, telemetry, and mission management
software stack.

## Status: Phases 1-6b complete (the full original roadmap) + a map search / vehicle spawn helper

See [`docs/PHASE_1_PLAN.md`](docs/PHASE_1_PLAN.md) for the Phase 1 plan and
[`docs/adr/`](docs/adr/) for the architecture decisions behind every
phase (Phase 2's mission protocol decision is ADR-0008, Phase 3's fleet
decisions are ADR-0009, Phase 4's real-PX4 validation is ADR-0010, Phase
5's API key auth is ADR-0011, Phase 6a's geofencing/failsafe is ADR-0012,
Phase 6b's risk monitoring/Remote ID is ADR-0013). There is no Phase 7 —
the roadmap is done; ADR-0014 (map search + vehicle spawn helper) is a
UX/tooling addition made after it, not a new phase.

Phase 1 delivers a working vertical slice end-to-end against a simulated
vehicle: telemetry flows from a simulator through a MAVLink gateway into a
backend API and a live map UI, and arm/takeoff commands flow back the other
way. Phase 2 adds waypoint missions (upload, start, live progress) and
return-to-launch, using the real MAVLink mission protocol so the simulator
can be swapped for a real autopilot later without a protocol change. Phase 3
scales that to a fleet: multiple vehicles, each with its own simulator and
gateway, all visible and commandable from one UI, with commands/missions
correctly routed to each vehicle's own gateway. Phase 4 put Phase 1's bet to
the test: a real, natively-built PX4 SITL was connected to this repo's
actual, completely unmodified `services/telemetry-gateway` — it worked, with
zero code changes, confirming HEARTBEAT/telemetry/ARM/TAKEOFF all function
against real autopilot firmware (see ADR-0010). No code in this repo changed
for Phase 4; the custom simulator remains the default for day-to-day
development. Phase 5 closes the biggest production gap left after that:
every vehicle-facing endpoint (commands, missions, telemetry, both
WebSocket routes) now requires a shared API key once one is configured —
opt-in and off by default, so none of Phases 1-4's zero-config testing
changes unless you explicitly set `API_KEY` (see
[`docs/adr/0011-api-authentication.md`](docs/adr/0011-api-authentication.md)).
Phase 6a adds a geofence per vehicle: draw one in the UI (or `POST
/vehicles/{id}/geofence`) and a mission with a waypoint outside it is
rejected up front, while a vehicle that ends up outside it while armed is
automatically sent RTL by a failsafe manager watching live telemetry (see
[`docs/adr/0012-geofencing-failsafe.md`](docs/adr/0012-geofencing-failsafe.md)).
Phase 6b closes out the rest of the original Phase 6 brief: a heuristic
risk monitor flags unusual telemetry (rapid battery drain, degraded GPS,
a sudden altitude drop, abnormal speed) as advisory `LOW`/`MEDIUM`/`HIGH`
flags an operator can see but that never auto-command the vehicle, and a
Remote ID simulator exposes the fields a real drone's identity/location
broadcast would carry (see
[`docs/adr/0013-risk-monitoring-and-remote-id.md`](docs/adr/0013-risk-monitoring-and-remote-id.md)).

```
simulation/sitl  --MAVLink/UDP-->  telemetry-gateway  --HTTP-->  api  <--WS/REST-->  gcs-web
   (simulator)                        (services/)              (services/)          (apps/)
```

## Quick start (no Docker required)

```bash
./scripts/run_local.sh
```

Brings up a 2-vehicle fleet by default (`FLEET_SIZE=1` for a single
vehicle, or higher for a bigger fleet). Then open
`http://localhost:8080/?api=http://localhost:8000` to see every vehicle on
the map and in the Fleet list — click one to select it, then arm it,
command takeoff, click the map to add waypoints and fly a mission, or send
it home with RTL.

### Quick start on Windows (PowerShell)

`scripts/run_local.sh` is a bash script and won't run natively in
PowerShell. There's a one-command equivalent that does the same 4 steps
for you — starts the API, gateway, simulator, and UI each in their own
new window, in the right order, waiting for each to come up before
starting the next:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1
```

Then open `http://localhost:8080/?api=http://localhost:8000`. Single
vehicle only — for a second vehicle, add the two extra terminals from
the "Want a second vehicle" section below by hand alongside it.

If you'd rather run (or debug) each piece yourself, here are the same
four components as separate terminals — **start them in this order**
(API → gateway → simulator), each `cd`'d into the service's own
directory (each service's package lives *inside* that directory — e.g.
the API's package is `app`, not `api`, and only resolves when the
working directory is `services/api`):

```powershell
# Terminal 1 -- API
cd services\api
.\..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Terminal 2 -- telemetry gateway (after the API is up)
cd services\telemetry-gateway
$env:API_BASE_URL = "http://127.0.0.1:8000"
.\..\..\.venv\Scripts\python.exe -m uvicorn gateway.main:app --host 0.0.0.0 --port 8001

# Terminal 3 -- simulator (after the gateway is up)
.\.venv\Scripts\python.exe simulation\sitl\sim.py --target-host 127.0.0.1 --target-port 14550

# Terminal 4 -- static GCS UI
cd apps\gcs-web
.\..\..\.venv\Scripts\python.exe -m http.server 8080
```

Then open the same URL as above. `Invoke-RestMethod http://127.0.0.1:8000/health`
should return `status: ok` once Terminal 1 is running.

**Want a second vehicle (Phase 3 fleet)?** Add two more terminals: a
second gateway on a different port/`VEHICLE_ID`, and a second simulator
pointed at it:

```powershell
# Terminal 5 -- second gateway
cd services\telemetry-gateway
$env:API_BASE_URL = "http://127.0.0.1:8000"
$env:VEHICLE_ID = "sim-2"
$env:GATEWAY_MAVLINK_PORT = "14551"
.\..\..\.venv\Scripts\python.exe -m uvicorn gateway.main:app --host 0.0.0.0 --port 8002

# Terminal 6 -- second simulator
.\.venv\Scripts\python.exe simulation\sitl\sim.py --target-host 127.0.0.1 --target-port 14551 --home-lat 37.4290 --home-lon -122.1680
```

Then restart Terminal 1 (the API) with `$env:GATEWAY_URLS_JSON =
'{"sim-2": "http://127.0.0.1:8002"}'` set first, so it knows to route
sim-2's commands to its own gateway (see
[`docs/adr/0009-multi-vehicle-fleet.md`](docs/adr/0009-multi-vehicle-fleet.md)).
Both vehicles will show up in the same browser tab's Fleet list — no
`vehicle=` query param needed.

### Want API authentication (Phase 5)?

Off by default — everything above works unchanged with no key. To turn
it on, set `API_KEY` on Terminal 1 (the API) before starting it:

```powershell
$env:API_KEY = "some-shared-secret"
```

Then open the UI with the key in the URL so it sends it on every request:
`http://localhost:8080/?api=http://localhost:8000&api_key=some-shared-secret`.
Without the key, `Invoke-RestMethod http://127.0.0.1:8000/vehicles` should
now fail with a `401`; with it
(`-Headers @{"X-API-Key"="some-shared-secret"}`) it should succeed. See
[`docs/adr/0011-api-authentication.md`](docs/adr/0011-api-authentication.md)
and `services/api/README.md` for the full endpoint list this covers.

### Try geofencing + auto-RTL (Phase 6a)

In the UI, with a vehicle selected: click **Draw** under Geofence, click
the map at least 3 times to outline a boundary, then **Save**. Two things
to try:

1. **Upload a mission with a waypoint outside that boundary** — the
   Mission panel should show `Upload FAILED` with a `waypoint(s) [...]
   fall outside the vehicle's geofence` error; nothing gets sent to the
   vehicle.
2. **Arm, take off, and fly (or manually push) the vehicle outside the
   fence** — the Telemetry panel's "Geofence" row should switch to
   `BREACHED — auto-RTL issued` in red, and the vehicle should start
   flying home on its own, with no RTL button click needed. It resets to
   `OK` once the vehicle disarms.

From PowerShell, the same check without the UI:
```powershell
Invoke-RestMethod http://127.0.0.1:8000/vehicles/sim-1/geofence -Method Post `
  -ContentType "application/json" `
  -Body '{"points": [{"lat":37.427,"lon":-122.170},{"lat":37.427,"lon":-122.168},{"lat":37.429,"lon":-122.168},{"lat":37.429,"lon":-122.170}]}'
```
Then try uploading a mission with a waypoint far outside those bounds —
it should come back as a `422` instead of `200`. See
[`docs/adr/0012-geofencing-failsafe.md`](docs/adr/0012-geofencing-failsafe.md)
for the full design.

### Try risk monitoring + Remote ID (Phase 6b)

With a vehicle selected, the Telemetry panel's **Risk** row updates
automatically as telemetry arrives — normally `LOW`; watch it change to
`MEDIUM`/`HIGH` with a flag name (e.g. `GPS_DEGRADED`) if you set
`gps_fix_type`/battery/speed to unusual values via a direct
`POST /internal/telemetry` call, or just let a real flight play out
(nothing in the simulator is scripted to trigger these, so most normal
flights should stay `LOW`). This never changes the vehicle's
behavior — it's purely informational, unlike the geofence failsafe.

Click **Fetch broadcast** under **Remote ID** to see the simulated
identity/location broadcast payload for the selected vehicle — try it
before and after a geofence breach and compare the `status.emergency`
field. From PowerShell:
```powershell
Invoke-RestMethod http://127.0.0.1:8000/vehicles/sim-1/remote_id
```
See [`docs/adr/0013-risk-monitoring-and-remote-id.md`](docs/adr/0013-risk-monitoring-and-remote-id.md)
for the full design and what this does/doesn't simulate.

### Search anywhere on the map / start a vehicle somewhere else

Type a place into the search box at the top of the map (e.g. "Mumbai",
"Heathrow Airport") and press Enter — the map recenters there. This
only changes what you're looking at; it doesn't move any vehicle.

To start a **new** simulated vehicle somewhere else: in the **Add a
Vehicle** panel, click **Pick location**, then click the map where you
want it to start. This fills in a suggested vehicle ID and ports, and
shows the exact commands to run in two new terminals — copy them,
adjusting the vehicle ID/ports if you already used them for another
vehicle. See
[`docs/adr/0014-map-search-and-vehicle-spawn-helper.md`](docs/adr/0014-map-search-and-vehicle-spawn-helper.md).

To start your **first** (single) vehicle somewhere other than the
Stanford default without using the UI, `run_local.ps1` also takes
optional coordinates:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1 -HomeLat 19.0760 -HomeLon 72.8777
```

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
| `docs/adr/` | Architecture decision records (ADR-0008: Phase 2 mission protocol; ADR-0009: Phase 3 fleet) |
| `simulation/sitl/` | Phase 1/2 MAVLink simulator (stand-in for PX4/ArduPilot SITL — see ADR-0001) |
| `services/telemetry-gateway/` | MAVLink ↔ internal HTTP bridge (one instance per vehicle, Phase 3) |
| `services/api/` | REST + WebSocket backend, persistence, fleet-wide `/ws/fleet` |
| `apps/gcs-web/` | Ground control station UI: fleet map/list + per-vehicle detail |
| `infra/` | Docker Compose for the full stack |
| `scripts/` | Local run + end-to-end smoke test |
| `.github/workflows/` | CI: lint, unit tests, integration smoke test |

Each service has its own `README.md` with endpoints/env vars/test commands.
