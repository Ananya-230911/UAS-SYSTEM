# ADR-0020: CBBA Phase E — Live Agents, Heartbeats, Lossy Radio, Failure Re-planning

## Status
Accepted (Phase E of `docs/CBBA_PLAN.md`)

## Context
Phases B–D planned in lock-step rounds in which every message arrives.
`docs/CBBA_PLAN.md` §2A asks for each drone to run its own
observe → communicate → decide → act → react loop, to cope with lost and
late messages, to *notice* failures itself (heartbeats), and to re-plan.
It must stay a simulation: no MAVLink, no vehicle, no claim of real
distributed consensus.

## Decision

**`app/sim/`** (server-side, in-process, deterministic for a given seed):
- `transport.py`: `LossyTransport`. Each message is lost with probability
  `message_loss` and arrives `message_delay_ticks` late. A drone whose
  radio is down when a message arrives never gets it. It reuses the
  existing `AgentMessage` model.
- `world.py`: `MissionSim`. One tick per `tick_s`. Each drone:
  - **observes** its position, battery and arriving messages;
  - **communicates** a heartbeat to the drones it can hear. The
    heartbeat carries its planning round, the places it knows are done,
    and the newest "alive" stamp it knows for every teammate, so
    liveness is *relayed* across a multi-hop network. While planning, it
    also sends its CBBA bids;
  - **decides** using the unchanged `CbbaAgent`: bundle building and
    Table I consensus over the lossy radio;
  - **acts**: flies its route, hovers to inspect, returns home;
  - **reacts** to these situations:

    | Situation | Reaction |
    |---|---|
    | No *newer* news of a teammate for `heartbeat_timeout_s` | Declare it lost and start a new planning round for the places not yet done |
    | Battery can no longer cover its places plus the trip home (the validator's endurance check) | Drop its places, head home, start a new round |
    | Every contact goes silent at once | **Lost-link**: finish its own places, take no new work |
    | Only the last remaining teammate goes silent | Assume it is the last drone and re-plan alone |
    | It rejoins after the others re-planned without it | Return home rather than repeat work |
- `models.py`: `SimConfig`, `FailureEvent` (`drone_lost`, `radio_cut`,
  `radio_restored`, `battery_drop`), frames, events, epoch records and
  summary.

**Safety stays deterministic and identical.** Every plan, first or
re-plan, goes through `app/cbba/service.py::build_result`, which is the
same safety gate as `allocate()`. `allocate()` was refactored into
`split_inputs` and `build_result` so both paths share it; nothing is
duplicated. A plan that isn't agreed within `max_planning_ticks` is
blocked (`NOT_CONVERGED`) and the drones go home. Altitude bands are
fixed for the whole mission, so a re-plan never puts two drones at one
height, even one flying without radio contact.

**Honest limits.**
- The simulator decides when a planning round has converged (everyone
  in contact agrees, and nothing changed for D + delay + 1 ticks). That
  is a simulation convenience, labelled `CBBA-async-sim (in-process,
  simulated lossy radio)`. It is not real distributed consensus.
- Lost-link versus last-survivor is a heuristic. From inside a drone a
  broken radio and crashed teammates look the same. Both choices are
  safe; the cost is either double work or unfinished places, and both
  are reported.

**Server.**
- `POST /sim/run` takes the scenario, config and failures, and returns
  frames, events, planning rounds and a summary.
- `GET /fleet/live` is **read-only**. It reads current positions and
  batteries from `services/api` using GET only (the existing
  `app/demo.py::load_live_fleet`). Stale or incomplete vehicles are
  skipped and listed, never filled in. It returns 503 when the API is
  down.

**GCS.** Section 4 becomes "Live agents":
- set radio loss, delay and heartbeat timeout;
- schedule what goes wrong;
- run, then play, pause or scrub the replay. The map shows radio links
  in use, inspected (✓) and still-to-do (dashed) places, and drone
  status; the log fills in as time passes;
- "Make this drone fail now" re-runs with the same seed, so everything
  up to now replays identically and then diverges;
- "Use my running drones' positions" calls `/fleet/live`.

The old browser-only flight animation is removed and replaced by this.

## Bugs found while building, fixed
1. **Crash with lossy radio.** Drones that disagreed about which places
   were done crashed the plan building. The fix plans over every place
   any member still considers open.
2. **Cut-off drone never noticed.** A drone with a broken radio never
   realised it was cut off, so it now runs detection too.
3. **False alarms in multi-hop networks.** A drone was declared lost
   because it wasn't a *direct* neighbour, so heartbeats are now relayed.
4. **Relayed news looked stale.** Relayed heartbeats caused 3,055 false
   alarms across 40 random missions, because relayed news is always a
   few seconds old. The fix is the gossip failure-detector rule: a
   teammate is lost only when no *newer* news has arrived for the
   timeout. That brought it to 71: 65 from injected radio cuts
   (expected) and 6 from one 30%-loss, 2-second-delay mission.
5. **Wrong status labels.** Drones with no work were left labelled
   "planning" on the ground.
6. **Last survivor gave up.** Found by the browser test: after two
   teammates crashed at different times, the last drone wrongly assumed
   its own radio had failed and left places undone.

## How this was verified
- **fleet-agent: 529 passed, 1 skipped** (CrewAI not installed).
- **57 new simulation and API tests**, covering:
  - nominal;
  - deterministic replay;
  - a lost drone noticed after exactly 5 s of silence, with its places
    re-planned;
  - a broken radio, a restored radio, a sudden battery drop;
  - 30% loss with 1 s delay;
  - an un-agreeable plan blocked with nobody taking off;
  - fixed altitudes;
  - the last drone standing;
  - range-limited radio;
  - no LLM, network or flight imports;
  - 40 random missions (random radio, failures and fleets), all keeping
    every safety rule: always inside the fence, only plans that passed
    the gate are flown, and the run ends with nobody left mid-air.
- **JS: 21 passed.**
- **Headless Chromium:**
  - sim-2 lost at 40 s with 10% message loss: detected at 45 s,
    re-planned at 52 s, 9/9 reachable places done;
  - "make sim-3 fail now" at 70 s: sim-1 re-planned alone (3 rounds),
    still 9/9;
  - no page errors.
- **Against the real flight stack** (services/api, two gateways, two
  MAVLink simulators): "Use my running drones' positions" imported
  sim-1 and sim-2 and skipped a stale sim-3 with a note. The live
  simulation ran to completion. Afterwards both simulators were still
  **disarmed in STANDBY**, and the API log had **zero** command or
  mission requests.

## Consequences
- The simulation is computed in one go on the server and then replayed
  in the browser, so it isn't interactive in real time. "Fail now"
  re-runs with the same seed instead.
- There is still no real inter-process transport, so real distributed
  consensus is not claimed (rule 4). A UDP transport between processes
  remains optional future work.
