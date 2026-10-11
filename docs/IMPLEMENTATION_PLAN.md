# Implementation Plan: AI-Enabled Multi-Agent Module for UAS Functions

> Paste this whole file into Claude Code at the root of my UAS-SYSTEM repository.
> You can also save it in the repo as `docs/IMPLEMENTATION_PLAN.md` so it stays as a reference.

---

## 0. Your role and how to work with me

You are a senior robotics, multi-agent systems and full-stack engineer working inside my existing **UAS-SYSTEM** repository.

I am a student. This is a **university project, built and demonstrated in simulation only**.

How to work:

1. **Read my existing code first.** Do not rewrite working features. Extend them.
2. **Work one phase at a time.** At the end of each phase, stop, show me what changed, run the tests, report the *real* results, and wait for my "go" before the next phase.
3. **Explain decisions in simple English.**
4. **Never claim something works unless a test or a run shows it.**
5. **Use small, reviewable commits**, one per logical step.
6. **Never hardcode secrets.** Use `.env` and give me a `.env.example`.
7. **Simulation is the default.** Any real-hardware path must be off by default and behind an explicit config flag.

---

## 1. Project goal (in one paragraph)

Build a software module that lets **several simulated drones work together as cooperative AI agents**. The operator gives one high-level mission ("search this area"). The agents then plan routes, split the work, fly, search, detect objects of interest, share data over a simulated mesh network, and recover from failures (a drone with low battery or a lost link hands its work to the others). This happens on their own. A human operator at the **Ground Control Station (GCS)** watches everything live on a Leaflet map. The operator can **approve** the mission, **override** any drone, and **abort** everything at any time.

## 2. What is already in my repo (verify, don't assume)

- A web page with a **Leaflet map**.
- Picking a location (e.g. Chennai/Delhi) and drawing a **geofence**.
- Manual waypoint selection.
- A Python backend that talks to a **simulated drone via MAVLink/pymavlink**.
- Basic telemetry/status.

My geofence, map and MAVLink code become the **safety layer** and **GCS** of the new system. Reuse them.

## 3. Scope

### In scope (build these)

| # | Feature | What it means here |
|---|---|---|
| 1 | Autonomous mission planning | Turn one goal into per-drone routes, and replan when things change |
| 2 | Search & reconnaissance | Grid / lawnmower area search with live sensor data |
| 3 | Object detection | Detect and label objects (person, vehicle, building, etc.) in the simulated camera feed and **show them on the GCS map** |
| 4 | Multi-agent coordination | Roles (scout, surveillance, relay, coordinator), task sharing, failure recovery |
| 5 | Mesh communication | Drone-to-drone and drone-to-GCS messages that keep working when some links drop |
| 6 | Edge processing | Each drone's agent runs as its **own process**, as if onboard, and keeps working with limited GCS link |
| 7 | Human-in-the-loop | Approve, manual override, abort, and explainable alerts |
| 8 | Safety | Geofence, altitude limits, drone-to-drone separation, battery / link-loss failsafes, signed messages |

### Out of scope (do NOT build these)

- **No sensor-to-shooter link, no strike-support, no weapon or payload release logic, no target engagement, no targeting priority for attacks.** Detections only go to the GCS map and log for the human to see.
- No real hardware deployment, no Jetson flashing, no MIL-STD/ISO certification work. List these as "Future work" in the README only.

## 4. Technology choices

Prefer what my repo already uses. Defaults if nothing exists:

- **Simulator:** ArduPilot SITL, multiple instances (`sim_vehicle.py -I 0..N`, each on its own MAVLink port). If my repo already uses PX4 SITL, keep PX4.
- **Camera / vision sim (Phase 5):** Gazebo with a camera on each drone. **Fallback:** a "mock camera" that plays sample images/video, so the project still works on a weak laptop.
- **Backend:** Python 3.10+, FastAPI (or my existing framework), WebSockets for live updates.
- **Drone link:** pymavlink (reuse my existing gateway).
- **Agent messaging (simulated mesh):** a small Python message bus over UDP or ZeroMQ, with configurable link range, delay and packet loss. Messages are signed with HMAC.
- **Validation / schemas:** Pydantic.
- **Detection:** YOLOv8 via `ultralytics` (pretrained COCO model), with a mock detector for tests.
- **LLM (optional):** only to turn the operator's typed goal into a structured mission request. Use a provider interface plus a **deterministic fallback parser**, so everything works with no API key.
- **Tests:** pytest. Mock the MAVLink gateway in unit tests.
- **No** LangGraph / agent frameworks / vector DBs unless a real need appears. Plain Python classes and asyncio are enough.

## 5. Target architecture

```
┌──────────────────────── Ground Control Station (web) ─────────────────────────┐
│ Leaflet map: all drones, geofence, search grid, routes, detections            │
│ Mission panel: type goal → see plan → APPROVE / REJECT                        │
│ Per-drone cards: role, mode, battery, link, task │ Override │ ABORT ALL      │
│ Event log + AI explanations                                                   │
└──────────────────────────────▲─────────────────────────────▲─────────────────┘
                               │ WebSocket / REST            │
┌──────────────────────────────┴─────────────────────────────┴─────────────────┐
│ Backend (FastAPI)                                                              │
│  mission/   goal parser (LLM or fallback) → planner → safety validator        │
│  gcs/       approval, override, abort, event log                              │
│  comms/     mesh bus bridge (GCS is one node on the mesh)                     │
└──────────────────────────────▲────────────────────────────────────────────────┘
                               │ simulated mesh (signed messages, lossy links)
     ┌─────────────────────────┼─────────────────────────┐
┌────┴─────┐             ┌─────┴────┐              ┌─────┴────┐
│ Agent D1 │◄──mesh────► │ Agent D2 │ ◄──mesh────► │ Agent D3 │   (one process per drone = "edge")
│ role,    │             │          │              │          │
│ tasks,   │             │          │              │          │
│ detector │             │          │              │          │
└────┬─────┘             └────┬─────┘              └────┬─────┘
     │ pymavlink (via narrow gateway interface)          │
┌────┴─────┐             ┌────┴─────┐              ┌────┴─────┐
│ SITL #0  │             │ SITL #1  │              │ SITL #2  │
└──────────┘             └──────────┘              └──────────┘
```

**Key rules:**

- The AI/LLM **only proposes**. Deterministic code **validates**. The human **approves**.
- Agents **never send raw MAVLink**. They call a typed gateway (`goto`, `upload_mission`, `set_mode`, `rtl`, `land`, `get_telemetry`), and the gateway checks the geofence and limits on every command.
- **Abort from the GCS overrides everything**, including agent decisions.

## 6. Suggested folder layout (adapt to my repo)

```
backend/
  core/models.py            # Pydantic schemas (all messages, missions, telemetry)
  core/config.py            # settings from env / YAML
  gateway/mavlink_gateway.py   # reuse/extend my existing code
  gateway/fleet.py          # manages N gateways
  safety/geofence.py        # reuse my existing geofence logic
  safety/validator.py       # mission + command checks
  safety/failsafe.py        # battery, link loss, separation
  planning/coverage.py      # grid / lawnmower generation
  planning/allocation.py    # task allocation (auction)
  mission/goal_parser.py    # LLM provider + fallback
  mission/manager.py        # mission state machine
  comms/bus.py              # simulated mesh bus
  comms/security.py         # HMAC signing / replay protection
  agents/drone_agent.py     # per-drone agent loop
  agents/roles.py           # scout / surveillance / relay / coordinator
  perception/detector.py    # YOLO + mock
  perception/camera.py      # Gazebo or mock image source
  gcs/api.py                # REST + WebSocket routes
scripts/
  start_sitl_fleet.sh       # launch N SITL instances
  run_agents.py             # launch one agent process per drone
frontend/                   # extend my existing Leaflet page
tests/
docs/
```

---

## 7. Phases

Each phase lists: **goal → tasks → done when (acceptance) → tests**. Stop after each phase.

### Phase 0 — Audit my repo (no code changes)

**Tasks**
- Read the repo structure, README, frontend, Leaflet code, geofence, backend routes, MAVLink code, telemetry, configs, tests, dependencies and git status.
- Check whether SITL runs on my machine, and which simulator I use.

**Deliver a report with:**
A. What works (proven by code or a run)
B. Partially done
C. Missing compared with this plan
D. Current architecture and data flow
E. Files to modify
F. New files really needed
G. Risks (performance of running N SITLs, Gazebo on my laptop, Windows/WSL issues, etc.)
H. Any changes you suggest to this plan

**Done when:** I approve the report.

---

### Phase 1 — Multi-drone simulation + live fleet view

**Goal:** 3 simulated drones running and visible on my map.

**Tasks**
- `scripts/start_sitl_fleet.sh N` launches N SITL instances with different ports and start positions inside the geofence.
- `gateway/fleet.py` connects to all of them, using my existing gateway per drone.
- Telemetry per drone: position, altitude, heading, mode, armed, battery (if provided), GPS fix, last-update time.
- WebSocket pushes fleet telemetry to the frontend. The map shows each drone with its ID and colour.
- Show **stale** (no update > 3 s) and **disconnected** states clearly. Never fake values.
- Keep my old single-drone features working.

**Done when:** I run one script, open the page, and see 3 drones moving when I command them.
**Tests:** gateway mocked; stale detection; disconnect handling.

---

### Phase 2 — Data models + safety layer

**Goal:** One shared "language" for the system and a strict safety checker.

**Tasks**
- Pydantic models: `Waypoint`, `Geofence`, `SearchArea`, `Task`, `TaskAssignment`, `MissionRequest`, `MissionPlan`, `ValidationResult`, `ApprovalRecord`, `TelemetrySnapshot`, `AgentMessage`, `Detection`, `Event`.
- Safety validator:
  - coordinates valid, altitude within min/max
  - every waypoint **and every route segment** inside the geofence
  - each drone gets its own **altitude band** plus a minimum horizontal separation check between planned routes
  - battery / endurance estimate is enough for the route plus return home
  - launch/home points valid
- Every command to the gateway passes through the validator.
- Plans get a **hash**. Execution is refused if the plan changed after approval.

**Done when:** an invalid plan is rejected with a clear reason, and a valid one passes.
**Tests:** inside/outside/boundary points, segment crossing the fence, bad altitude, separation conflict, modified-after-approval.

---

### Phase 3 — Mission planning + task allocation

**Goal:** One goal becomes a plan for every drone.

**Tasks**
- `planning/coverage.py`: split the search area into cells, then generate a lawnmower path per cell (configurable spacing based on camera footprint).
- `planning/allocation.py`: **auction-based allocation**. Each drone "bids" on cells using distance plus remaining battery; the lowest cost wins. Start with a simple greedy auction and explain it in the docs.
- Roles: `coordinator` (runs the auction), `scout` (searches), `relay` (holds position to extend comms range), `surveillance` (orbits a point the operator selects).
- `mission/goal_parser.py`: operator text → `MissionRequest`. LLM provider if a key is set, otherwise a rule-based fallback. **LLM output is validated by Pydantic and the safety validator; if it is invalid, ask the operator instead of guessing.**
- `mission/manager.py` state machine: `DRAFT → VALIDATED → APPROVED → RUNNING → COMPLETED / FAILED / ABORTED`.
- GCS shows the proposed plan (cells coloured per drone, routes, relay positions) with an AI explanation ("D1 gets the north cells because it's closest and has 92% battery").

**Done when:** I type "search the area inside the geofence with 3 drones", see the plan on the map, approve it, and the drones fly their parts in SITL.
**Tests:** coverage completeness (no gaps), allocation is fair and deterministic, malformed LLM output, LLM provider down → fallback.

---

### Phase 4 — Agents, mesh comms and failure recovery

**Goal:** Drones act as cooperative agents and fix problems without a human.

**Tasks**
- `agents/drone_agent.py`: one **separate process per drone** (simulates onboard edge compute). The loop does: read telemetry → check tasks → publish status → handle messages → act through the gateway.
- `comms/bus.py`: simulated mesh with
  - per-link range limit (based on distance between drones), delay and packet loss (configurable)
  - **multi-hop relay**: if D3 can't reach the GCS directly, its messages go via D2
  - heartbeats every 1 s
- `comms/security.py`: HMAC-signed messages, sequence numbers to block replays, rejection of unsigned/old messages.
- Failure handling (clear rules, each logged and explained on the GCS):
  - **Low battery** → drone announces "leaving", returns home, and the coordinator **re-auctions** its unfinished cells.
  - **Lost heartbeat** from a drone for X s → others treat it as lost and reassign its cells. The lost drone's own failsafe triggers RTL.
  - **Coordinator lost** → the next drone by ID becomes coordinator (simple leader election).
  - **GCS link lost** → agents **continue the already-approved mission** but do not accept new missions. RTL after a configurable timeout.
  - **Separation conflict** → the lower-priority drone holds position until clear.
- A GCS "chaos" panel (for the demo): kill a drone, drain its battery, cut a link, add packet loss.

**Done when:** in the demo, I kill one drone mid-mission, and the others finish its area automatically while the GCS log explains what happened.
**Tests:** re-auction after failure, leader election, message signing / replay rejection, multi-hop delivery, GCS-link-loss behaviour.

---

### Phase 5 — Object detection

**Goal:** Drones detect objects in their camera feed and report them.

**Tasks**
- `perception/camera.py`: Gazebo camera topic per drone **or** a mock source (folder of sample aerial images / a video).
- `perception/detector.py`: YOLOv8 pretrained model (person, car, truck, etc.). Run at a limited FPS to mimic edge limits. Add a mock detector for tests.
- Convert the detection to an approximate **map position** using the drone position, altitude, heading and camera angle (simple pinhole projection; document the error).
- Merge duplicate detections from multiple drones (same class, within X m).
- Send `Detection` (class, confidence, position, drone ID, time, image snapshot) over the mesh to the GCS.
- GCS: markers on the map, a list with thumbnails, and confidence filter.
- Optional: the operator clicks a detection → "send nearest drone to take a closer look" (a surveillance orbit), **only with operator confirmation**.
- **Detections are for display and logging only. No link to any targeting or engagement logic.**

**Done when:** a detected object appears on the map near its real place in the sim.
**Tests:** projection maths, duplicate merging, mock detector pipeline end-to-end.

---

### Phase 6 — Ground Control Station polish + human-in-the-loop

**Tasks**
- Mission panel: goal text box, plan preview, validation results, **Approve / Reject**.
- Per-drone card: role, mode, battery, link quality, current task, **Override** (RTL / land / hold / go-to point inside the fence).
- **ABORT ALL** button (big, always visible) → all drones RTL; the agents must obey.
- Event timeline: every agent decision with a one-line human explanation.
- Clear status colours: draft / validated / approved / running / completed / failed / aborted.
- Mission replay from the log (nice for the demo).

**Done when:** I can run a full mission from the browser only, and abort works within 1–2 s.
**Tests:** override and abort win over agent decisions; actions not allowed in the current state are refused.

---

### Phase 7 — Testing, docs and demo

**Tasks**
- Run all unit and integration tests and report real results.
- A `make demo` (or a single script) that starts the SITL fleet, agents, backend and frontend.
- README: setup (Linux/WSL), config, architecture diagram, how each feature works, how to run the demo, troubleshooting, limitations, **future work** (Jetson deployment, real radios, certification standards like ISO/IEC 42001, ISO/IEC 27001, IEC 61508, MIL-STD-810H).
- `docs/DEMO_SCRIPT.md`: a step-by-step 5-minute demo:
  1. Start 3–4 drones
  2. Type a search goal → show the plan and explanation → approve
  3. Drones split and search; detections appear
  4. Kill one drone → others take over its area
  5. Cut a link → multi-hop relay keeps data flowing
  6. Press ABORT ALL → all return home

---

## 8. Required tests (summary)

Valid plan · invalid coordinates · waypoint outside fence · segment crossing fence · bad altitude · separation conflict · plan modified after approval · execution without approval · duplicate execute requests · stale telemetry · disconnected SITL · LLM down / malformed LLM output · re-auction after drone loss · leader election · signed-message rejection / replay · multi-hop relay · GCS link loss · abort overrides agents · detection projection · duplicate detection merge.

Unit tests must run **without** SITL or a GPU (use mocks). Integration tests may need SITL and must be marked so they can be skipped.

## 9. Coding rules

1. Read before you change. 2. Keep working features working. 3. Small commits. 4. Type hints everywhere. 5. Clear error messages. 6. No placeholder code claimed as finished. 7. Don't remove existing endpoints or UI silently. 8. Document every new config option and dependency. 9. Simulation by default. 10. The AI proposes, code validates, the human approves, and abort always wins.

## 10. Start now

**Do Phase 0 only.** Give me the audit report and wait for my approval before Phase 1.
