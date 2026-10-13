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
