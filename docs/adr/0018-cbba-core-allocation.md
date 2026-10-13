# ADR-0018: CBBA Core — Decentralised Inspection-Point Allocation (simulation)

## Status
Accepted (Phase B of `docs/CBBA_PLAN.md`)

## Context
`docs/CBBA_PLAN.md` asks for simulated drones to split **inspection
points** among themselves using the Consensus-Based Bundle Algorithm
(Choi, Brunet & How, IEEE T-RO 2009). It must be deterministic Python,
with no LLM anywhere in allocation or safety, kept separate from the
MAVLink flight-command pipeline, and honestly labelled as an in-process
simulation. Phase A found nothing in the repo that already does this:
`app/planning/allocation.py` is a central auction over coverage *cells*
with a different cost, so it is neither CBBA nor a valid reference for it.

## Decision

**New package `services/fleet-agent/app/cbba/`**, alongside (not
replacing) the Phase 3 planner:

| File | Role |
|---|---|
| `models.py` | `InspectionPoint`, `DroneSpec`, `CommsSpec`, `CbbaConfig`, `Scenario`, `DronePlan`, `CbbaResult`; reuses `Waypoint`, `Geofence`, `SafetyConstraints`, `ValidationResult` |
| `scoring.py` | Time-discounted path score (paper eq. 11), best-insertion marginal gain, tie-break rule |
| `agent.py` | One drone: bundle phase, Table I consensus, bundle release (eq. 6), timestamps (eq. 5) |
| `network.py` | `CommsGraph` (explicit links or radio range), `Transport` protocol, `InMemoryTransport` |
| `runner.py` | Synchronous rounds until stable for D rounds *and* every connected group agrees |
| `sga.py` | Centralised Sequential Greedy reference (the test oracle; never dispatched) |
| `safety_gate.py` | Deterministic post-allocation checks; any issue blocks dispatch |
| `service.py` | `allocate(scenario, method="CBBA" \| "SGA") -> CbbaResult` — the one entry point |
| `demo.py` | `python -m app.cbba.demo` — terminal demo, simulation only |

**Reuse, not duplication.**
- Endurance is checked with `app/safety/validator.py::check_endurance`,
  both while bidding and in the safety gate. Bidding can therefore never
  be more optimistic than the check that blocks dispatch.
- The gate also calls `check_altitude`, `check_route_against_fence`
  (segments included, so concave fences are handled),
  `check_launch_point` and `check_separation`.
- Altitude bands come from `assign_altitude_bands`.
- Distances use `horizontal_distance_m`.

**Scoring.** `S(path) = Σ value · λ^arrival_s`, with λ = 0.999/s by
default.
- A path is infeasible if a deadline is missed or the battery would go
  below the reserve (including the flight home).
- A drone may only bid on points whose sensor (`required_capability`) it
  carries.
- Ties: higher bid wins, then lower drone id, then lower point id.

**Bid warping (a deviation from the 2009 paper, found by testing).**
- The paper's convergence proof needs *diminishing marginal gain* (DMG).
  It treats the time-discounted reward as DMG, but **with best-insertion
  routing it is not**.
- Two nearby points are cheaper together, so a point can be worth more
  after another one is added.
- A randomised check during this phase found a 5-drone multi-hop case
  that cycled for 500 rounds. Point p01 was traded between two drones:
  bid 39.9, then 75.2 after a neighbour point was added, and back again.
  The safety gate correctly blocked it (`NOT_CONVERGED`,
  `DUPLICATE_ASSIGNMENT`), but the algorithm was wrong.
- **Fix: bid warping** (Johnson, Choi, Ponda & How, "Allowing
  non-submodular score functions in distributed task allocation", IEEE
  CDC 2012; later BW-CBBA).
  - A drone still *chooses* by true marginal gain.
  - The bid it *publishes* is capped at its earlier bids, so published
    bids always shrink along a bundle. That restores convergence.
- SGA applies the same warping rule, so the two remain comparable.

**Safety gate** (`safety_gate.py`). It blocks dispatch on any of these:
- `NOT_CONVERGED`
- `DUPLICATE_ASSIGNMENT`
- `CAPABILITY_MISMATCH`
- `UNKNOWN_DRONE` / `UNKNOWN_POINT`
- `DRONE_OFFLINE`
- `ALTITUDE_OUT_OF_RANGE`
- `WAYPOINT_OUTSIDE_FENCE` / `SEGMENT_CROSSES_FENCE` (the legs from and
  back to home are included)
- `LAUNCH_POINT_OUTSIDE_FENCE`
- `INSUFFICIENT_ENDURANCE`
- `DEADLINE_MISSED` (recomputed independently of the bidding code)
- `SEPARATION_CONFLICT`
- `NO_AVAILABLE_DRONES`

`dispatch_allowed` is `converged AND safety.valid`. Nothing can override
it.

**Input filtering** (deterministic, with stated reasons):
- Offline drones, and drones starting outside the fence, don't take
  part.
- Points outside the fence are ignored.
- Points no drone can do are listed as unassigned, with the reason.

**Honest labelling.**
- CBBA results are labelled `CBBA-sync-sim (in-process)`.
- SGA results are labelled `SGA (centralised reference, not dispatched)`.
- No claim of real distributed consensus is made.

## How this was verified
- The existing 85 fleet-agent tests still pass.
- A randomised check (scratch script, not yet in CI; it becomes Phase C)
  ran 400 random scenarios:
  - 1–6 drones and 1–18 points;
  - random sensors, values, deadlines, hover times, batteries and bundle
    limits;
  - half fully connected, half random connected multi-hop graphs.

  Results after the fix:

  | Check | Result |
  |---|---|
  | Converged | 400/400 |
  | Conflict-free (no point given to two drones) | 400/400 |
  | Safety gate passed | 400/400 |
  | Identical output when re-run (deterministic) | 400/400 |
  | Settled within the paper's bound, N_min·D rounds (N_min = min(points, drones × bundle limit)) | 400/400 |

- **Correction made in Phase C: CBBA does not always equal SGA.**
  - Phase B reported 200/200 identical to SGA on fully connected graphs
    and 198/200 on multi-hop ones. Phase C's larger random set found a
    fully connected case that differs (random case 44 in
    `tests/cbba/test_allocate.py`).
  - **Cause.** Drones bid simultaneously. In round 1, d2 bid on p06
    while also holding p02, which it lost to d0 in that same round.
    d0 believed d2's bid, gave up p06, and filled its last bundle slot
    with p08. d2's real bid on p06 turned out lower, but d0's bundle was
    full and nothing outbid p08, so d0 never revisited p06.
  - The result is still converged, conflict-free and safe, just a
    different allocation from SGA's.
  - **Measured on 600 + 600 random cases:**

    | Comms | Identical to SGA | Worst CBBA score vs SGA | CBBA higher than SGA |
    |---|---|---|---|
    | Fully connected | 593/600 | 96.4% | 4 cases |
    | Multi-hop | 592/600 | 97.3% | 2 cases |

  - **What the tests now assert:**
    - On every case, the guarantee that follows from the paper:
      CBBA ≥ 50% of SGA (CBBA ≥ 50% of optimal, and SGA ≤ optimal).
    - Exact equality only for the sample scenario.
    - A measured floor: at least 95% identical on the fixed test set.
- The safety gate was hand-checked against tampered plans. Each one was
  blocked with the right code:
  - a thermal point given to a camera-only drone;
  - a point outside the fence;
  - a duplicated point with a late deadline;
  - altitude set to 500 m;
  - battery set to 21%;
  - an unconverged run.

## Consequences
- No HTTP server, storage, CrewAI, Groq, agent loop or dashboard yet.
  Those are Phases D–F.
- Nothing here imports `pymavlink`, calls `services/api`, or sends any
  command.
- **Known limitation: synchronous rounds.** Every drone acts in lock-step
  inside one process. Message loss, delay and heartbeats arrive with the
  sim agents in Phase E.
- **Known limitation: split comms.** If the comms graph is split, each
  group agrees internally but two groups can claim the same point. The
  gate then blocks dispatch with `DUPLICATE_ASSIGNMENT`. That is correct
  behaviour, but no plan is produced until the groups reconnect.
- **Known limitation: lock-step separation** (unchanged from ADR-0016).
  Each drone has its own altitude band, which is the real deconfliction.
- **Known limitation: speed.** A path is rescored for every insertion
  position and every round. That is fine at demo scale (well under a
  second for 3 drones and 11 points) but has not been tuned for large
  fleets.

## Phase C: tests (added later)
`services/fleet-agent/tests/cbba/`, run by the existing CI fleet-agent job
(`PYTHONPATH=. pytest tests/`). 324 tests, about 5 seconds, no network, no
LLM, no simulator.

| File | What it covers |
|---|---|
| `test_consensus.py` | Every cell of Table I; bundle release (eq. 6); timestamps (eq. 5); bid warping on a layout where the true gain really rises, with a check that warping fired |
| `test_scoring_and_network.py` | Score formula; hover time; deadlines; endurance identical to the validator at the reserve boundary; sensor rule; insertion position; tie-break table; comms graph (full / range / links / cuts / diameter); transport drops unreachable messages |
| `test_safety_gate.py` | Every block reason on hand-made plans; the battery-reserve boundary both ways |
| `test_allocate.py` | See the list below |

`test_allocate.py` covers:
- the sample scenario;
- unassigned and ignored points with their reasons;
- sensors and deadlines respected;
- a lost drone's points reallocated;
- relayed comms giving the same answer;
- split comms blocked;
- edge cases: no drones, out of reach, bundle limit, round limit,
  duplicate ids;
- 120 fixed random cases checked for: converged, agreement,
  conflict-free, safe, deterministic, within N_min·D rounds, and ≥ 50%
  of SGA's score;
- a measured floor of ≥ 95% identical to SGA;
- the frozen cycling case (`data/cycling_without_bid_warping.json`):
  it converges, and it provably cycles again if warping is switched off;
- partition and merge, a partition that never heals, and no early stop
  before a scheduled link change;
- the demo in all four modes;
- `app/cbba` imports no LLM, network or flight library (rules 1 and 5,
  enforced).

The runner gained optional `link_events` (cut or restore links at given
rounds) for the partition/merge tests. Phase E's comms-failure scenarios
will reuse it.

## Map preview (moved forward at the operator's request)
After Phase C the operator asked to see the work in the GCS UI. So a
read-only slice of Phase D (server) and Phase F (map panel) was built
early.

**Server: `services/fleet-agent/app/server.py`** (FastAPI, port 8020).
- Endpoints:
  - `GET /health`
  - `GET /cbba/sample?comms=&offline=`, which returns the scenario, the
    result and the SGA comparison;
  - `POST /cbba/allocate`.
- No command endpoints; a test enforces this.
- No storage yet. `services/api` is untouched.
- Launcher: `scripts/run_cbba.ps1`.

**Map panel: `apps/gcs-web/cbba.js`**, plus a new "Multi-drone
(simulation)" section in `index.html`. It draws on its own Leaflet
layer:
- the geofence;
- each point, coloured by the drone that won it (white = not assigned,
  grey = outside fence, red = claimed twice);
- each drone's route;
- drone start positions (offline drones crossed out).

The panel shows:
- the safety verdict;
- the honest "simulation only" label;
- a plain-English sentence on how agreement was reached (including
  split radio networks);
- who inspects what, what isn't inspected and why;
- block reasons, grouped by code.

The pure logic has Node tests (`tests/cbba.test.js`), and CI now also
syntax-checks `cbba.js`.

**Bug found by the UI, fixed.** "Chain comms + sim-2 offline" crashed the
server: the chain's links still named the removed drone.
- Fix: a drone that takes no part relays nothing, so its links are
  dropped. Taking out the middle relay therefore splits the network, and
  the gate blocks the plan.
- `Scenario` now rejects links that name unknown drones.
- `CbbaResult.comms_groups` reports the radio groups.
- Regression tests: `test_offline_relay_drone_breaks_the_chain`,
  `test_chain_with_offline_relay_does_not_crash`.

**Verified in a real browser** (headless Chromium driving the page):
- Three runs (normal, sim-3 cut off, chain with sim-2 offline) each
  drew 11 point markers and 3 drone markers.
- The panel showed the correct verdicts.
- No page errors.
- Map tiles were not loaded in the sandbox, which has no internet
  access.

## Mission editor, decision replay and simulated flight (operator request)
The operator asked to choose the places to inspect themselves, anywhere
in the world, and to *see* the agents communicate and decide.

**GCS panel (`apps/gcs-web/cbba.js`)** now has four sections:

1. **Mission setup.** Click the map to place drones and places (in any
   city), and drag them to move.
   - Each drone: name, battery, sensors (camera / thermal / lidar),
     "offline", "radio broken".
   - Each place: name, sensor needed, priority (low 50 / normal 100 /
     high 150, i.e. the CBBA value), and an optional deadline.
   - Radio: everyone can talk, or limited range in metres.
   - Fence: drawn by the operator, or an automatic box around everything.
   - Saved in the browser (localStorage).
   - The Stanford example loads on first use.
2. **Plan with CBBA.** `POST /cbba/plan`, followed by the existing safety
   gate.
3. **How the drones decided.** A replay with two steps per round:
   - "thinking alone": the bids, with contested places in red;
   - "talking": the radio links drawn, the message count, and every
     give-up in plain English (outbid by whom and by how much, dropped
     because it came after a lost place, or reset).
4. **Simulated flight.** The drones move along the planned routes in the
   browser, and places are ticked off as they are inspected.
   - **Make this drone fail**: the remaining drones re-plan with CBBA
     from where they are now, with the battery used so far deducted and
     inspected places removed. They then continue flying the new plan.
   - A blocked re-plan stops the simulation with the reason shown.

**Server.**
- `POST /cbba/plan` takes an operator-built `Scenario`.
- `/cbba/sample` and `/cbba/plan` also return `trace`: one `TraceRound`
  per round (links, messages, wants, claims, events). It is recorded by
  the runner and capped at 60 rounds.
- `DroneSpec.home` is optional; it is used when re-planning mid-mission
  (start = current position, the drone must return home).
- The battery check for a mid-mission re-plan uses the validator's
  `check_endurance` with [current position] + route. That also counts the
  home → here leg, which has already been flown. This is conservative
  (it can refuse a feasible plan, never accept an infeasible one), and a
  test documents it.

**Bug found by the browser test, fixed.** `app.js` already has a global
`droneIcon`. A function with the same name in `cbba.js` crashed the
script, so the panel's browser code now runs in its own function scope.
`app.js`'s map-click handler ignores clicks while the CBBA panel is
placing things, so no stray mission waypoints are added.

**Verified.**
- Tests: fleet-agent 429; JS 20.
- A scripted headless-Chromium run with unpkg.com blocked:
  1. The Stanford example loaded and planned (PASSED).
  2. The replay explained round 1.
  3. In flight at 25× speed, sim-2 failed at t = 42 s. Its remaining
     place P06 was re-planned to sim-1 (PASSED). All drones came home,
     with 9 of 11 places inspected: P10 needs lidar, and P11 is outside
     the fence.
  4. A new mission with 2 drones and 5 places, built by clicking near
     Chennai, planned and PASSED.
  5. No stray waypoints were added, and there were no page errors.
- **Not done here:** typed instructions to an AI assistant ("inspect the
  water tank with thermal"). That is Phase D (Groq goal parsing plus the
  intent gate). The LLM will only fill in the mission form; it never
  computes the plan.
