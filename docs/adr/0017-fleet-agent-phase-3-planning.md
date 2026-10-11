# ADR-0017: Fleet-Agent Phase 3 — Mission Planning + Task Allocation

## Status
Accepted

## Context
Phase 3 of `docs/IMPLEMENTATION_PLAN.md`: one operator goal ("search the
area inside the geofence with 3 drones") must become a plan for every
drone — coverage routes, a fair split of the work, roles, and a mission
lifecycle — with the AI limited to *interpreting* the request and
deterministic code doing everything else. Built on ADR-0016's models and
safety validator, in the same separate `services/fleet-agent` service.

The plan's own "done when" for Phase 3 also mentions seeing the plan on
the map and flying it. Those belong to its Phase 4 (frontend) and Phase 5
(execution); this phase delivers the planning itself, plus a read-only
terminal demo (`app/demo.py`) so it can be seen working against a live
fleet without touching the GCS or sending a command.

## Decision

**Coverage (`app/planning/coverage.py`).** A rectangular grid over the
search area's bounding box, at least two cells per drone, keeping cells
whose centre is inside the area. Each cell gets a lawnmower path whose
line spacing comes from the camera footprint
(`2 · altitude · tan(fov/2) · (1 − overlap)`, 60° FOV and 20% overlap by
default). Lines are spread so every point is within half a spacing of a
line, and stop half a spacing short of the cell's sides — the camera
still reaches the edge, and a path never runs exactly along a cell edge
that may be the geofence itself. Tested: a rectangular area is tiled with
no gaps or overlaps; a fine grid of points in a cell is all covered.

**Allocation (`app/planning/allocation.py`) — a greedy sequential
auction, chosen because it can be explained in four sentences and tested
exactly.** Each round every drone bids on every open cell; cost =
(distance already committed + distance to the cell) × (100 / battery %);
the cheapest bid wins; ties break by vehicle id then cell id. Drones at
or below the reserve battery don't bid. The sweep length of each cell is
a *required* input: an early version let it default to zero, and the
tests showed one drone then chained neighbouring cells (a 3/2/1 split for
three identical drones). With it required, identical drones split 2/2/2.

**Roles.** The lowest-id participating drone is COORDINATOR (and still
searches); others are SCOUT. RELAY and SURVEILLANCE are defined in the
model but not assigned — they need the mesh (Phase 4) and an
operator-picked point (Phase 6).

**Altitude bands.** Each drone flies `requested altitude + i × 5 m`. The
validator checks the altitudes actually written in the routes — see the
correction to ADR-0016 below.

**Goal parsing (`app/mission/goal_parser.py`).** A small provider
interface with two implementations: a rule-based parser (default; no
network, no key) and `ClaudeGoalParser`, used only when
`ANTHROPIC_API_KEY` is set. Claude fills a fixed Pydantic schema via
structured outputs (`client.messages.parse(..., output_format=ParsedGoal)`)
— intent, number of drones, altitude — and nothing else. Rules applied to
any provider: out-of-scope requests (weapons, attacks) are refused before
a provider runs; provider unavailable or refusing → rule-based fallback,
reported in the result; malformed output or "unclear" → a question for
the operator, never a guess; more drones requested than available → a
question. The `anthropic` package is imported lazily and is not in
`requirements.txt`, so the core of this service has no dependency on it.

**Lifecycle (`app/mission/manager.py`).** DRAFT → VALIDATED → APPROVED →
RUNNING → COMPLETED/FAILED, and ABORTED from any unfinished state. An
invalid plan stays DRAFT. `start()` refuses unless APPROVED *and* the plan
still hashes to what was approved. Each step is logged as an `Event`.

**Planner (`app/mission/planner.py`).** Ties the above together and
returns the plan, its validation result, and plain-English explanations
of who got which cells and why. Drones whose launch point is outside the
geofence are left out with that reason; drones that win no cells are not
part of the plan.

## Corrections to Phase 2 (ADR-0016), found while building this
- **Separation used assigned bands, not real altitudes.** A plan could
  pass with two drones' waypoints at the same height. `check_separation()`
  now compares each pair of corresponding waypoints' actual altitudes;
  regression test `test_separation_uses_real_waypoint_altitudes_not_assigned_bands`.
- **Only drones with routes were checked.** A drone named in the plan
  (e.g. as coordinator) with no waypoints skipped the launch-point check —
  found when a drone launching from Delhi was named coordinator of a
  Stanford mission. The validator now checks every drone the plan names.
- **Unknown battery was assumed to be 100%.** Now flagged as
  `MISSING_BATTERY` instead.

## How this was verified
- 85 unit tests, all passing (coverage, auction fairness/determinism,
  parser fallback/clarification/out-of-scope, planner, lifecycle,
  tampering, abort).
- The demo was run against a real 3-drone fleet (`run_local.sh`,
  `FLEET_SIZE=3`) with a geofence saved through `services/api`: no fence →
  it asks for one; with a fence → a validated, approved plan; after one
  simulator was killed → that drone was skipped as disconnected and a
  request for 3 drones became a question.
- The Claude path was run against the real Anthropic API with a
  deliberately invalid key: 401, then rule-based fallback, reported in
  the output. A successful Claude call was **not** tested (no key in this
  environment); it is covered by the SDK's real method signature and a
  fake client in the tests.

## Consequences
- **Known limitation — lockstep separation** (unchanged from ADR-0016).
- **Known limitation — grid cells.** On a non-rectangular area a kept
  cell can poke outside the fence; the plan then fails validation rather
  than being trimmed.
- **Known limitation — the auction balances route length**, not
  geographic neighbourhoods.
- **Known limitation — the rule-based parser is keyword matching.**
- No command is sent anywhere. Map display is Phase 4; execution through
  `services/api`'s existing endpoints is Phase 5.
