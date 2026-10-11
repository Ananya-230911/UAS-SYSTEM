import pytest

from app.core.models import (
    ExecutionStatus,
    Geofence,
    GeofencePoint,
    MissionRequest,
    SafetyConstraints,
    SearchArea,
    VehicleRole,
    Waypoint,
)
from app.mission.manager import IllegalTransition, MissionManager, PlanChangedSinceApproval
from app.mission.planner import plan_mission
from app.planning.allocation import VehicleState
from app.safety.validator import validate_mission_plan

FENCE = Geofence(
    points=[
        GeofencePoint(lat=37.4250, lon=-122.1720),
        GeofencePoint(lat=37.4250, lon=-122.1650),
        GeofencePoint(lat=37.4310, lon=-122.1650),
        GeofencePoint(lat=37.4310, lon=-122.1720),
    ]
)
VEHICLES = {
    "sim-1": VehicleState("sim-1", Waypoint(lat=37.4275, lon=-122.1697, alt_m=0), 96.0),
    "sim-2": VehicleState("sim-2", Waypoint(lat=37.4275, lon=-122.1684, alt_m=0), 92.0),
    "sim-3": VehicleState("sim-3", Waypoint(lat=37.4275, lon=-122.1671, alt_m=0), 61.0),
}
CONSTRAINTS = SafetyConstraints()


def _request(vehicle_ids=("sim-1", "sim-2", "sim-3"), altitude_m=20.0):
    return MissionRequest(
        goal_text="search", vehicle_ids=list(vehicle_ids), search_area=SearchArea(points=FENCE.points), altitude_m=altitude_m
    )


def _homes_and_battery(plan):
    ids = plan.mission_request.vehicle_ids
    return {v: VEHICLES[v].home for v in ids}, {v: VEHICLES[v].battery_pct for v in ids}


# --- planner ---


def test_three_drone_search_produces_a_valid_plan():
    result = plan_mission(_request(), FENCE, CONSTRAINTS, VEHICLES)
    assert result.validation.valid, [i.message for i in result.validation.issues]
    assert {a.vehicle_id for a in result.plan.assignments} == {"sim-1", "sim-2", "sim-3"}


def test_every_cell_is_covered_by_exactly_one_drone():
    result = plan_mission(_request(), FENCE, CONSTRAINTS, VEHICLES)
    cell_ids = [t.cell_id for t in result.plan.tasks]
    assert len(cell_ids) == len(set(cell_ids)) == 6


def test_each_drone_flies_its_own_altitude_band():
    result = plan_mission(_request(), FENCE, CONSTRAINTS, VEHICLES)
    by_vehicle = {a.task_id: a.vehicle_id for a in result.plan.assignments}
    altitudes = {}
    for t in result.plan.tasks:
        altitudes.setdefault(by_vehicle[t.task_id], set()).update(wp.alt_m for wp in t.waypoints)
    assert altitudes == {"sim-1": {20.0}, "sim-2": {25.0}, "sim-3": {30.0}}


def test_roles_and_explanations_are_present():
    result = plan_mission(_request(), FENCE, CONSTRAINTS, VEHICLES)
    assert result.plan.roles["sim-1"] == VehicleRole.COORDINATOR
    assert any("sim-3" in line and "61% battery" in line for line in result.explanations)


def test_planning_is_deterministic_apart_from_the_new_plan_id():
    request = _request()  # one request: its requested_at timestamp is part of the plan's content
    a = plan_mission(request, FENCE, CONSTRAINTS, VEHICLES).plan
    b = plan_mission(request, FENCE, CONSTRAINTS, VEHICLES).plan
    assert a.plan_id != b.plan_id
    assert a.content_hash() == b.content_hash()


def test_altitude_outside_limits_is_rejected_not_clamped():
    result = plan_mission(_request(altitude_m=500.0), FENCE, CONSTRAINTS, VEHICLES)
    assert result.plan is None
    assert [i.code for i in result.validation.issues] == ["ALTITUDE_OUT_OF_RANGE"]


def test_low_battery_drone_is_left_out_with_a_reason():
    vehicles = dict(VEHICLES, **{"sim-3": VehicleState("sim-3", VEHICLES["sim-3"].home, 15.0)})
    result = plan_mission(_request(), FENCE, CONSTRAINTS, vehicles)
    assert "sim-3" not in {a.vehicle_id for a in result.plan.assignments}
    assert any(line.startswith("sim-3 was left out") for line in result.explanations)


def test_drone_launching_outside_the_fence_is_left_out_with_a_reason():
    # Regression: this drone used to win zero cells, so its out-of-fence
    # position was never checked -- and it was still named coordinator.
    outside = Waypoint(lat=28.6139, lon=77.2090, alt_m=0)  # Delhi, far outside a Stanford fence
    vehicles = dict(VEHICLES, **{"sim-1": VehicleState("sim-1", outside, 96.0)})
    result = plan_mission(_request(), FENCE, CONSTRAINTS, vehicles)
    assert "sim-1" not in result.plan.roles
    assert "sim-1" not in result.plan.mission_request.vehicle_ids
    assert result.plan.roles["sim-2"] == VehicleRole.COORDINATOR
    assert "sim-1 was left out: its launch point is outside the geofence." in result.explanations
    assert result.validation.valid


def test_every_drone_outside_the_fence_means_no_plan():
    outside = Waypoint(lat=28.6139, lon=77.2090, alt_m=0)
    vehicles = {vid: VehicleState(vid, outside, 96.0) for vid in VEHICLES}
    result = plan_mission(_request(), FENCE, CONSTRAINTS, vehicles)
    assert result.plan is None
    assert result.validation.issues[0].code == "NO_ELIGIBLE_VEHICLES"


def test_validator_checks_a_named_drone_even_if_it_has_no_route():
    # Defense in depth: even if a planner bug names an out-of-fence drone
    # as coordinator with no tasks, the validator must still reject it.
    plan = plan_mission(_request(vehicle_ids=("sim-2", "sim-3")), FENCE, CONSTRAINTS, VEHICLES).plan
    plan.roles["sim-1"] = VehicleRole.COORDINATOR
    outside = Waypoint(lat=28.6139, lon=77.2090, alt_m=0)
    homes = {"sim-1": outside, "sim-2": VEHICLES["sim-2"].home, "sim-3": VEHICLES["sim-3"].home}
    result = validate_mission_plan(plan, FENCE, CONSTRAINTS, homes, {"sim-2": 92.0, "sim-3": 61.0})
    assert not result.valid
    assert {"vehicle_id": "sim-1", "code": "LAUNCH_POINT_OUTSIDE_FENCE"} in [
        {"vehicle_id": i.vehicle_id, "code": i.code} for i in result.issues
    ]


def test_unknown_battery_is_flagged_not_assumed_full():
    plan = plan_mission(_request(), FENCE, CONSTRAINTS, VEHICLES).plan
    homes, battery = _homes_and_battery(plan)
    del battery["sim-2"]
    result = validate_mission_plan(plan, FENCE, CONSTRAINTS, homes, battery)
    assert [i.code for i in result.issues] == ["MISSING_BATTERY"]


def test_unknown_vehicle_is_rejected():
    result = plan_mission(_request(vehicle_ids=("sim-1", "sim-9")), FENCE, CONSTRAINTS, VEHICLES)
    assert result.plan is None
    assert result.validation.issues[0].code == "UNKNOWN_VEHICLE"


# --- mission state machine ---


def _validated_manager():
    plan = plan_mission(_request(), FENCE, CONSTRAINTS, VEHICLES).plan
    manager = MissionManager(plan)
    homes, battery = _homes_and_battery(plan)
    assert manager.validate(FENCE, CONSTRAINTS, homes, battery).valid
    return manager


def test_happy_path_draft_to_completed():
    m = _validated_manager()
    m.approve("operator")
    m.start(m.plan)
    m.complete()
    assert m.status == ExecutionStatus.COMPLETED
    assert [e.event_type for e in m.events] == [
        "MISSION_DRAFTED", "MISSION_VALIDATED", "MISSION_APPROVED", "MISSION_RUNNING", "MISSION_COMPLETED",
    ]


def test_cannot_start_without_approval():
    m = _validated_manager()
    with pytest.raises(IllegalTransition):
        m.start(m.plan)


def test_cannot_approve_an_invalid_plan():
    plan = plan_mission(_request(), FENCE, CONSTRAINTS, VEHICLES).plan
    m = MissionManager(plan)
    homes, battery = _homes_and_battery(plan)
    battery = {v: 21.0 for v in battery}  # far too little for ~8 km routes
    assert not m.validate(FENCE, CONSTRAINTS, homes, battery).valid
    assert m.status == ExecutionStatus.DRAFT
    with pytest.raises(IllegalTransition):
        m.approve("operator")


def test_plan_modified_after_approval_is_refused():
    m = _validated_manager()
    m.approve("operator")
    tampered = m.plan.model_copy(deep=True)
    tampered.tasks[0].waypoints[0] = Waypoint(lat=37.5, lon=-122.5, alt_m=20.0)
    with pytest.raises(PlanChangedSinceApproval):
        m.start(tampered)
    assert m.status == ExecutionStatus.APPROVED
    assert m.events[-1].event_type == "MISSION_START_REFUSED"


def test_duplicate_start_is_refused():
    m = _validated_manager()
    m.approve("operator")
    m.start(m.plan)
    with pytest.raises(IllegalTransition):
        m.start(m.plan)


@pytest.mark.parametrize("steps", [[], ["approve"], ["approve", "start"]])
def test_abort_wins_from_any_unfinished_state(steps):
    m = _validated_manager()
    if "approve" in steps:
        m.approve("operator")
    if "start" in steps:
        m.start(m.plan)
    m.abort("operator pressed ABORT")
    assert m.status == ExecutionStatus.ABORTED


def test_nothing_happens_after_a_mission_has_finished():
    m = _validated_manager()
    m.abort()
    with pytest.raises(IllegalTransition):
        m.abort()
    with pytest.raises(IllegalTransition):
        m.approve("operator")
