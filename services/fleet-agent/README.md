# services/fleet-agent

The multi-agent fleet module from `docs/IMPLEMENTATION_PLAN.md` — a new,
separate service (same reasoning as `services/perception`: keep
experimental/heavier pieces out of the stable core `services/api`).

**Status: Phase 2 of the plan only.** Data models (`app/core/models.py`)
and the deterministic safety validator (`app/safety/`). No HTTP server,
no AI/LLM, no execution wiring, no mesh comms, no agents yet — those
are later phases. See `docs/adr/0016-fleet-agent-phase-2-models-and-safety.md`.

## What's here

- `app/core/models.py` — Pydantic schemas: `Waypoint`, `Geofence`,
  `SearchArea`, `SafetyConstraints`, `Task`, `TaskAssignment`,
  `MissionRequest`, `MissionPlan` (with a content-hash method),
  `ValidationIssue`/`ValidationResult`, `ApprovalRecord`,
  `ExecutionStatus`, `TelemetrySnapshot`, `AgentMessage`, `Detection`,
  `Event`.
- `app/safety/geofence.py` — point-in-polygon and segment-vs-polygon
  geometry (deliberately duplicated from `services/api/app/geofence.py`
  rather than imported across services, the same pattern
  `docs/adr/0006-internal-messaging.md` already established).
- `app/safety/validator.py` — `validate_mission_plan()`: altitude
  limits, every waypoint *and* every route segment against the
  geofence, launch-point validity, an endurance/battery estimate for
  the route plus return home, and inter-vehicle separation (altitude
  bands + minimum horizontal distance). Plus `make_approval_record()`/
  `plan_matches_approval()` — a plan is hashed at approval time, and
  execution (once Phase 5 exists) must refuse anything that doesn't
  hash-match what was actually approved.

## Run the tests

```bash
cd services/fleet-agent
pip install -r requirements.txt
PYTHONPATH=. pytest tests/ -v
```

## Known limitations (Phase 2 scope)

- **Separation check assumes routes progress in lockstep** (same list
  index treated as "same time"). A real time-synced check needs
  per-waypoint ETAs, which Phase 3's coverage/allocation (not built
  yet) doesn't compute. Documented, not silently assumed.
- **Thresholds are demonstration constants** (`SafetyConstraints`'
  defaults), not validated against real flight data — the same caveat
  `services/api/app/risk_monitor.py` already carries. `cruise_speed_ms`
  and `battery_drain_pct_per_min` are set to match
  `simulation/sitl/sim.py`'s own constants so the endurance estimate
  is at least consistent with what the simulator will actually do.
- **Nothing here is wired to a live vehicle or gateway yet.** This
  phase is pure, testable logic — integration is Phase 5.
