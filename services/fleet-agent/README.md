# services/fleet-agent

The multi-agent fleet module from `docs/IMPLEMENTATION_PLAN.md` — a new,
separate service (same reasoning as `services/perception`: keep
experimental/heavier pieces out of the stable core `services/api`).

**Status: Phases 2 and 3 of the plan.** One typed data model, a strict
safety validator, and mission planning (goal text → plan → validation →
approval). It **sends no commands**: flying an approved plan is Phase 5,
and showing the plan on the map is Phase 4. See
`docs/adr/0016-fleet-agent-phase-2-models-and-safety.md` and
`docs/adr/0017-fleet-agent-phase-3-planning.md`.

## See it working

```powershell
cd services\fleet-agent
..\..\.venv\Scripts\pip.exe install -r requirements.txt
$env:PYTHONPATH = "."

# Built-in sample: 3 drones and a square geofence near Stanford
..\..\.venv\Scripts\python.exe -m app.demo "search the area inside the geofence with 3 drones"

# Your live fleet (read-only -- it never sends a command). Needs
# run_fleet.ps1 / run_local.ps1 running AND a geofence drawn + saved in
# the GCS (Draw -> click 3+ points -> Save) around where the drones are.
..\..\.venv\Scripts\python.exe -m app.demo "search the area with 3 drones at 25 m" --api http://localhost:8000
```

Things to try: `"hello drones"` (it asks what you mean), `"search with 5
drones"` (it asks, rather than guessing), `"survey at 500 m"` (the safety
layer rejects it), or stop one drone's simulator and plan again (it is
skipped as disconnected — never planned with stale data).

## CBBA: drones split inspection points among themselves (simulation)

Phase B of `docs/CBBA_PLAN.md` (see `docs/adr/0018-cbba-core-allocation.md`).
Simulated drones bid for places to inspect and agree on who goes where using
CBBA -- no central planner, no LLM. A deterministic safety gate then checks
the result; anything that fails is blocked. Nothing is flown.

```powershell
cd services\fleet-agent
$env:PYTHONPATH = "."
..\..\.venv\Scripts\python.exe -m app.cbba.demo                    # everyone can talk
..\..\.venv\Scripts\python.exe -m app.cbba.demo --comms line       # sim-1 <-> sim-2 <-> sim-3
..\..\.venv\Scripts\python.exe -m app.cbba.demo --comms split      # sim-3 cut off -> blocked
..\..\.venv\Scripts\python.exe -m app.cbba.demo --offline sim-2    # one drone down
```

**See it on the map (GCS page):**

```powershell
# one-time: .venv\Scripts\pip.exe install -r services\fleet-agent\requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\run_all.ps1       # flight stack + CBBA server + page, then checks them
# or only the CBBA server:  powershell -ExecutionPolicy Bypass -File scripts\run_cbba.ps1
```
Open `http://localhost:8080/` and scroll the right-hand panel to
**Multi-drone (simulation)**: 1 set up the mission by clicking the map (any
city), 2 **Run CBBA**, 3 replay how the drones decided, 4 fly it
(simulated) and make a drone fail to watch them re-plan. Works with or
without the flight simulators running. `app/server.py` is read-only:
`GET /health`, `GET /cbba/sample`, `POST /cbba/plan` (with a round-by-round
`trace`), `POST /cbba/allocate` -- no command endpoints.

| File | What it does |
| --- | --- |
| `app/server.py` | Read-only HTTP API on port 8020 for the GCS panel |
| `app/cbba/models.py` | Inspection points, drones (with sensors), comms, config, results |
| `app/cbba/scoring.py` | Time-discounted score; best place to insert a point into a route |
| `app/cbba/agent.py` | One drone's CBBA: build bundle, consensus (paper Table I), release lost points; bid warping |
| `app/cbba/network.py` | Comms graph and in-memory message transport (labelled simulation) |
| `app/cbba/runner.py` | Runs rounds until the drones agree |
| `app/cbba/sga.py` | Centralised greedy reference answer, used to check CBBA |
| `app/cbba/safety_gate.py` | Fence, altitude, battery, sensor, deadline, duplicate checks -- reuses `app/safety/validator.py` |
| `app/cbba/service.py` | `allocate(scenario)` -- the single entry point |

## Live agents (Phase E)

In the GCS panel, section **4 · Live agents**: choose how bad the radio is
(lost / late messages), schedule what goes wrong (a drone crashes, a radio
breaks or comes back, a battery drops), and press **Run live simulation**.
Every drone runs its own loop -- heartbeats, CBBA bids over the lossy radio,
noticing silent teammates, re-planning -- and you can scrub through the
replay or press **Make this drone fail now**. **Use my running drones'
positions** reads (GET only) where your `run_fleet.ps1` simulators are.
Server: `app/sim/`, `POST /sim/run`, `GET /fleet/live`. See
`docs/adr/0020-cbba-phase-e-live-agents.md`.

## Tell the drones (Phase D, optional AI)

In the GCS panel, type e.g. *"P3 is high priority, P2 needs thermal, D2 is
offline"* and press **Understand**. Built-in rules read it (offline, no key);
with a Groq key, Groq reads it. Either way the result is only a list of
*requested* changes: a fixed-rule check (`app/supervisor/gate.py`) accepts or
refuses each one, you press **Apply and re-plan**, CBBA re-plans and the
safety gate decides. Every plan is saved as a mission version
(`GET /missions`).

```powershell
# optional: use Groq (free key from https://console.groq.com/keys)
$env:GROQ_API_KEY = "gsk_..."          # never commit it; see .env.example
# optional: the CrewAI supervisor (large install)
..\..\.venv\Scripts\pip.exe install -r requirements-crew.txt
```
See `docs/adr/0019-cbba-phase-d-instructions-groq-crewai-store.md`.

## What's here

| File | What it does |
| --- | --- |
| `app/core/models.py` | Pydantic schemas shared by every phase (missions, tasks, roles, approvals, telemetry, messages, detections, events) |
| `app/safety/geofence.py` | Point-in-polygon and segment-crossing geometry |
| `app/safety/validator.py` | `validate_mission_plan()`: altitude, every waypoint *and* route segment inside the fence, every named drone's launch point, endurance, inter-drone separation; plan hashing for approval |
| `app/planning/coverage.py` | Split the area into cells; a lawnmower path per cell, line spacing from the camera footprint |
| `app/planning/allocation.py` | Greedy sequential auction — how it works is explained step by step at the top of the file |
| `app/mission/goal_parser.py` | Operator text → `MissionRequest`. Rule-based by default; Claude if `ANTHROPIC_API_KEY` is set; asks the operator when unsure |
| `app/mission/planner.py` | `plan_mission()`: ties coverage + auction + roles + altitude bands + validation together, with plain-English explanations |
| `app/mission/manager.py` | Lifecycle: `DRAFT → VALIDATED → APPROVED → RUNNING → COMPLETED/FAILED/ABORTED`; refuses illegal jumps and plans changed after approval |
| `app/demo.py` | The terminal demo above |

## Optional: let Claude interpret requests

Off by default — everything works without it.
```powershell
..\..\.venv\Scripts\pip.exe install anthropic
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```
Claude (`claude-opus-5-5`) only fills a fixed schema (intent, number of
drones, altitude). It never sees positions, picks routes, or sends
commands; its output is validated, and anything unclear becomes a
question for you. If it's unreachable or declines, the rule-based parser
takes over and the demo says so. See `.env.example`; never commit a key.

## Tests

```bash
cd services/fleet-agent
PYTHONPATH=. pytest tests/ -v
```
No simulator, GPU, network, or API key needed. The CBBA tests alone:
`PYTHONPATH=. pytest tests/cbba -q` (324 tests, ~5 s).

## Known limitations

- **Separation check assumes routes progress in lockstep** (same list
  index treated as "same time") — needs per-waypoint ETAs to be exact.
- **Cells are a rectangular grid** over the area's bounding box; for a
  non-rectangular area, a cell is kept if its centre is inside. A kept
  cell that pokes outside the fence makes the plan *fail validation*
  (correct, but you'd need to redraw the area).
- **The auction balances route length, not neighbourhoods** — a drone
  can get two cells far apart if that evens out the work.
- **Thresholds are demonstration constants** (camera FOV 60°, 20%
  overlap, `SafetyConstraints` defaults), not validated on real flights.
- **The rule-based parser is keyword matching** — it understands common
  phrasings, not arbitrary language. That's what the optional Claude
  provider is for.
