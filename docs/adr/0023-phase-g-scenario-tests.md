# ADR-0023: Phase G - scripted scenario tests

Status: accepted (October 2026)

## Decision

`services/fleet-agent/app/scenarios.py` holds 11 scripted end-to-end
scenarios. Each drives the real pipeline (CBBA, safety gate, live-agent
simulation, instruction gate, one-line survey) and checks the outcome
against the project's rules. The same code runs as tests
(`tests/scenarios/test_phase_g.py`) and writes the results report:

    cd services/fleet-agent
    python -m app.scenarios --report ../../docs/PHASE_G_RESULTS.md

| Key | Scenario | What must hold |
| --- | --- | --- |
| G1 | Nominal | agreement, gate passes, no duplicate, lidar/fence exclusions with reasons, >= 50% of SGA, all done once, home above 20% reserve, deterministic, own altitude bands |
| G2 | Drone loss at 60 s | noticed within timeout + 2 s, new plan passes gate, all reachable places done once, survivors home |
| G3 | Radio cut 40-140 s | lost-link procedure, others re-plan, contact restored, all done; double work only on the cut-off drone's own places; separate altitudes |
| G4 | Battery drop to 15% at 50 s | drone drops what it can't cover and goes home, re-plan agrees and passes gate, others take over all places |
| G5 | Fence change | places outside excluded with reason, every route point inside the new fence, nothing outside inspected; a fence that leaves the drones outside blocks |
| G6 | 30% message loss, 1 s late | agreement, all places done once |
| G7 | Radio partition | duplicate claims -> gate blocks -> nothing flies |
| G8 | Multi-hop radio | agreement via relay, all done, no false alarms |
| G9 | Instruction "P3 high priority, sim-2 offline" | both accepted by the fixed-rule gate, re-plan excludes sim-2, only allowed change kinds |
| G10 | Attack / targeting wording | refused before any AI runs (instructions and survey) |
| G11 | One-line survey | Delhi, Potheri, Sydney: gate passes, all points done once |

## Findings

1. **Bug found and fixed (G4).** When a drone re-planned after a battery
   drop, each drone built its CBBA task list from what IT knew was done.
   A place finished in the same second (P05) was on one drone's list and
   not another's, so their bids could never match: the re-plan ran 200
   ticks without agreement, the safety gate (correctly) blocked it, and 4
   of 9 places were never done. Fix (`app/sim/world.py`): every member of
   a planning round bids over the round's single shared list (CBBA's
   assumption); places any member knows are done are removed from the
   routes at dispatch. Verified: on the old code G4 fails (200 ticks, 5
   of 9); on the new code it passes (6 ticks, 9 of 9). All 571 earlier
   tests still pass.
2. **Accepted trade-off (G3).** A drone whose radio fails looks exactly
   like a crashed drone. The others re-plan its places while it finishes
   them under the lost-link procedure, so 2 places are done twice. This is
   safe (each drone keeps its own altitude band) and is reported, not
   hidden.
3. **Unsafe plans never fly (G7, G5).** A partitioned radio or a fence
   that excludes the launch points produces a blocked plan and nothing is
   flown.

## Results (this sandbox, October 2026)

11 of 11 scenarios, 48 of 48 checks passed; fleet-agent test suite 583
passed, 1 skipped (CrewAI not installed). Full numbers and event
timelines: `docs/PHASE_G_RESULTS.md` (generated, not hand-written).
