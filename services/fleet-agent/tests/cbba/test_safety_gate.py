"""The deterministic safety gate: every block reason, on hand-made plans.

These plans are deliberately tampered with or wrong -- the gate must not
trust anything about how a plan was produced.
"""
from app.cbba.models import DroneHealth, DronePlan
from app.cbba.safety_gate import check_allocation
from app.core.models import SafetyConstraints
from tests.cbba.helpers import FENCE, drone, point

C = SafetyConstraints()
DRONES = {
    "a": drone("a", 37.4270, -122.1700, caps=["camera", "thermal"]),
    "b": drone("b", 37.4270, -122.1680, caps=["camera"]),
}
POINTS = {
    "p1": point("p1", 37.4280, -122.1700),
    "p2": point("p2", 37.4280, -122.1680),
    "hot": point("hot", 37.4290, -122.1690, required_capability="thermal"),
    "late": point("late", 37.4305, -122.1655, deadline_s=20),
    "out": point("out", 37.4330, -122.1690),
}


def plan(drone_id, path, alt=20.0):
    return DronePlan(drone_id=drone_id, altitude_m=alt, path=path, bundle=path, arrival_s=[], score=0, route_m=0, route=[])


def codes(plans, drones=DRONES, converged=True):
    result = check_allocation(plans, drones, POINTS, FENCE, C, converged)
    assert result.valid == (not result.issues)
    return sorted({i.code for i in result.issues})


def test_good_plan_passes():
    assert codes([plan("a", ["p1", "hot"], 20), plan("b", ["p2"], 25)]) == []


def test_not_converged_blocks_even_a_good_plan():
    assert codes([plan("a", ["p1"])], converged=False) == ["NOT_CONVERGED"]


def test_point_given_to_two_drones():
    assert codes([plan("a", ["p1"], 20), plan("b", ["p1"], 25)]) == ["DUPLICATE_ASSIGNMENT"]


def test_wrong_sensor():
    assert codes([plan("b", ["hot"])]) == ["CAPABILITY_MISMATCH"]


def test_unknown_drone_and_unknown_point():
    assert codes([plan("ghost", ["p1"])]) == ["UNKNOWN_DRONE"]
    assert codes([plan("a", ["nope"])]) == ["UNKNOWN_POINT"]


def test_offline_drone_with_work():
    drones = {**DRONES, "a": DRONES["a"].model_copy(update={"health": DroneHealth.OFFLINE})}
    assert codes([plan("a", ["p1"])], drones=drones) == ["DRONE_OFFLINE"]


def test_altitude_out_of_range():
    assert codes([plan("a", ["p1"], alt=500)]) == ["ALTITUDE_OUT_OF_RANGE"]


def test_point_outside_the_fence():
    assert codes([plan("a", ["out"])]) == ["SEGMENT_CROSSES_FENCE", "WAYPOINT_OUTSIDE_FENCE"]


def test_drone_starting_outside_the_fence():
    drones = {**DRONES, "a": drone("a", 37.4400, -122.1700, caps=["camera"])}
    got = codes([plan("a", ["p1"])], drones=drones)
    assert "LAUNCH_POINT_OUTSIDE_FENCE" in got
    assert "SEGMENT_CROSSES_FENCE" in got  # the leg from home crosses the fence too


def test_not_enough_battery_for_route_and_return():
    # p1 is ~110 m away: the round trip needs ~0.3%, so 20.1% ends below
    # the 20% reserve (20.5% would just pass -- checked separately below).
    drones = {**DRONES, "a": DRONES["a"].model_copy(update={"battery_pct": 20.1})}
    assert codes([plan("a", ["p1"])], drones=drones) == ["INSUFFICIENT_ENDURANCE"]


def test_just_enough_battery_passes():
    drones = {**DRONES, "a": DRONES["a"].model_copy(update={"battery_pct": 20.5})}
    assert codes([plan("a", ["p1"])], drones=drones) == []


def test_missed_deadline():
    assert codes([plan("a", ["p1", "late"])]) == ["DEADLINE_MISSED"]


def test_two_drones_at_the_same_height_and_place():
    # Same first stop at the same height (the check compares waypoints in
    # order -- the documented lock-step simplification).
    assert codes([plan("a", ["p1"], 20), plan("b", ["p1"], 20)]) == [
        "DUPLICATE_ASSIGNMENT",
        "SEPARATION_CONFLICT",
    ]
