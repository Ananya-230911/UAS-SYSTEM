from app.core.models import (
    Geofence,
    GeofencePoint,
    MissionPlan,
    MissionRequest,
    SafetyConstraints,
    Task,
    TaskAssignment,
    Waypoint,
)
from app.safety.geofence import point_in_polygon, segment_crosses_fence_boundary
from app.safety.validator import (
    assign_altitude_bands,
    check_altitude,
    check_endurance,
    check_launch_point,
    check_route_against_fence,
    check_separation,
    make_approval_record,
    plan_matches_approval,
    validate_mission_plan,
)

# A simple square geofence, ~1.1km per side at this latitude.
SQUARE_FENCE = Geofence(
    points=[
        GeofencePoint(lat=37.420, lon=-122.175),
        GeofencePoint(lat=37.420, lon=-122.165),
        GeofencePoint(lat=37.430, lon=-122.165),
        GeofencePoint(lat=37.430, lon=-122.175),
    ]
)

# A concave "U" shape: a solid base bar (lat 0-2, full width) with two
# prongs rising from it (lat 2-10) separated by an empty gap (lon 3-7).
# The straight line between a point in the left prong and a point in
# the right prong crosses straight through that gap even though both
# endpoints are themselves inside the polygon.
CONCAVE_FENCE_POLYGON = [
    {"lat": 0.0, "lon": 0.0},
    {"lat": 0.0, "lon": 10.0},
    {"lat": 10.0, "lon": 10.0},
    {"lat": 10.0, "lon": 7.0},
    {"lat": 2.0, "lon": 7.0},
    {"lat": 2.0, "lon": 3.0},
    {"lat": 10.0, "lon": 3.0},
    {"lat": 10.0, "lon": 0.0},
]


def test_point_inside_polygon():
    assert point_in_polygon(37.425, -122.170, [p.model_dump() for p in SQUARE_FENCE.points])


def test_point_outside_polygon():
    assert not point_in_polygon(37.5, -122.0, [p.model_dump() for p in SQUARE_FENCE.points])


def test_point_on_boundary_is_treated_as_inside():
    # Same fail-open-on-the-line choice as services/api/app/geofence.py.
    assert point_in_polygon(37.420, -122.170, [p.model_dump() for p in SQUARE_FENCE.points])


def test_segment_between_two_inside_points_can_still_cross_a_concave_fence():
    # Both endpoints are inside the U-shape (left prong and right
    # prong, each confirmed individually inside), but the straight
    # line between them cuts straight through the empty gap.
    p1 = {"lat": 8.0, "lon": 1.5}
    p2 = {"lat": 8.0, "lon": 8.5}
    assert point_in_polygon(p1["lat"], p1["lon"], CONCAVE_FENCE_POLYGON)
    assert point_in_polygon(p2["lat"], p2["lon"], CONCAVE_FENCE_POLYGON)
    assert segment_crosses_fence_boundary(p1, p2, CONCAVE_FENCE_POLYGON)


def test_segment_fully_inside_a_convex_fence_does_not_cross():
    p1 = {"lat": 37.422, "lon": -122.172}
    p2 = {"lat": 37.428, "lon": -122.168}
    assert not segment_crosses_fence_boundary(p1, p2, [p.model_dump() for p in SQUARE_FENCE.points])


def test_bad_altitude_is_rejected_with_a_clear_reason():
    constraints = SafetyConstraints(min_altitude_m=5.0, max_altitude_m=50.0)
    route = [Waypoint(lat=37.425, lon=-122.170, alt_m=200.0)]
    issues = check_altitude(route, constraints, "sim-1")
    assert len(issues) == 1
    assert issues[0].code == "ALTITUDE_OUT_OF_RANGE"
    assert "sim-1" in issues[0].message


def test_altitude_within_range_passes():
    constraints = SafetyConstraints(min_altitude_m=5.0, max_altitude_m=50.0)
    route = [Waypoint(lat=37.425, lon=-122.170, alt_m=20.0)]
    assert check_altitude(route, constraints, "sim-1") == []


def test_waypoint_outside_fence_is_rejected():
    route = [Waypoint(lat=38.0, lon=-121.0, alt_m=20.0)]
    issues = check_route_against_fence(route, SQUARE_FENCE, "sim-1")
    assert any(i.code == "WAYPOINT_OUTSIDE_FENCE" for i in issues)


def test_launch_point_outside_fence_is_rejected():
    home = Waypoint(lat=38.0, lon=-121.0, alt_m=0.0)
    issues = check_launch_point(home, SQUARE_FENCE, "sim-1")
    assert len(issues) == 1
    assert issues[0].code == "LAUNCH_POINT_OUTSIDE_FENCE"


def test_launch_point_inside_fence_passes():
    home = Waypoint(lat=37.425, lon=-122.170, alt_m=0.0)
    assert check_launch_point(home, SQUARE_FENCE, "sim-1") == []


def test_insufficient_endurance_is_rejected():
    # A real round-trip distance (~1.1km each way, home -> waypoint ->
    # home) combined with a fast drain rate and a low starting charge
    # leaves less than the required reserve.
    constraints = SafetyConstraints(cruise_speed_ms=8.0, battery_drain_pct_per_min=5.0, battery_reserve_pct=20.0)
    home = Waypoint(lat=37.420, lon=-122.175, alt_m=0.0)
    route = [Waypoint(lat=37.430, lon=-122.175, alt_m=20.0)]
    issues = check_endurance(route, home, constraints, battery_pct=25.0, vehicle_id="sim-1")
    assert len(issues) == 1
    assert issues[0].code == "INSUFFICIENT_ENDURANCE"


def test_adequate_endurance_passes():
    constraints = SafetyConstraints(cruise_speed_ms=8.0, battery_drain_pct_per_min=0.6, battery_reserve_pct=20.0)
    home = Waypoint(lat=37.425, lon=-122.170, alt_m=0.0)
    route = [Waypoint(lat=37.4251, lon=-122.1701, alt_m=20.0)]  # a few meters away
    issues = check_endurance(route, home, constraints, battery_pct=100.0, vehicle_id="sim-1")
    assert issues == []


def test_altitude_bands_are_deterministic_and_spaced():
    constraints = SafetyConstraints(min_altitude_m=10.0, altitude_band_spacing_m=5.0, max_altitude_m=100.0)
    bands = assign_altitude_bands(["sim-2", "sim-1"], constraints)
    assert bands["sim-1"] == 10.0
    assert bands["sim-2"] == 15.0


def test_separation_conflict_detected_when_routes_are_close_and_coplanar():
    constraints = SafetyConstraints(altitude_band_spacing_m=5.0, min_horizontal_separation_m=10.0)
    # Same altitude band (both index 0 in sorted order would differ,
    # so force them onto the same band by giving identical ids' worth
    # of spacing -- simplest is to call check_separation directly with
    # equal bands).
    routes = {
        "sim-1": [Waypoint(lat=37.425000, lon=-122.170000, alt_m=20.0)],
        "sim-2": [Waypoint(lat=37.425001, lon=-122.170001, alt_m=20.0)],  # ~0.1m away
    }
    altitude_bands = {"sim-1": 20.0, "sim-2": 20.0}  # not vertically deconflicted
    issues = check_separation(routes, altitude_bands, constraints)
    assert len(issues) == 1
    assert issues[0].code == "SEPARATION_CONFLICT"


def test_no_separation_conflict_when_vertically_deconflicted():
    constraints = SafetyConstraints(altitude_band_spacing_m=5.0, min_horizontal_separation_m=10.0)
    routes = {
        "sim-1": [Waypoint(lat=37.425000, lon=-122.170000, alt_m=20.0)],
        "sim-2": [Waypoint(lat=37.425001, lon=-122.170001, alt_m=25.0)],  # same spot, different band
    }
    altitude_bands = {"sim-1": 20.0, "sim-2": 25.0}  # exactly altitude_band_spacing_m apart
    assert check_separation(routes, altitude_bands, constraints) == []


def test_no_separation_conflict_when_far_enough_apart_horizontally():
    constraints = SafetyConstraints(altitude_band_spacing_m=5.0, min_horizontal_separation_m=10.0)
    routes = {
        "sim-1": [Waypoint(lat=37.420, lon=-122.175, alt_m=20.0)],
        "sim-2": [Waypoint(lat=37.430, lon=-122.165, alt_m=20.0)],  # far away
    }
    altitude_bands = {"sim-1": 20.0, "sim-2": 20.0}
    assert check_separation(routes, altitude_bands, constraints) == []


def _valid_plan():
    task1 = Task(task_id="t1", cell_id="c1", waypoints=[Waypoint(lat=37.424, lon=-122.172, alt_m=20.0)])
    task2 = Task(task_id="t2", cell_id="c2", waypoints=[Waypoint(lat=37.428, lon=-122.168, alt_m=25.0)])
    return MissionPlan(
        plan_id="plan-1",
        mission_request=MissionRequest(goal_text="search", vehicle_ids=["sim-1", "sim-2"]),
        tasks=[task1, task2],
        assignments=[
            TaskAssignment(task_id="t1", vehicle_id="sim-1", bid_cost=1.0),
            TaskAssignment(task_id="t2", vehicle_id="sim-2", bid_cost=1.0),
        ],
    )


def test_validate_mission_plan_passes_for_a_valid_plan():
    plan = _valid_plan()
    constraints = SafetyConstraints()
    homes = {
        "sim-1": Waypoint(lat=37.4225, lon=-122.1725, alt_m=0.0),
        "sim-2": Waypoint(lat=37.4225, lon=-122.1725, alt_m=0.0),
    }
    battery = {"sim-1": 100.0, "sim-2": 100.0}

    result = validate_mission_plan(plan, SQUARE_FENCE, constraints, homes, battery)
    assert result.valid is True
    assert result.issues == []


def test_validate_mission_plan_rejects_with_clear_reasons():
    plan = _valid_plan()
    # Corrupt one task's waypoint to be outside the fence.
    plan.tasks[0].waypoints[0] = Waypoint(lat=38.0, lon=-121.0, alt_m=20.0)
    constraints = SafetyConstraints()
    homes = {
        "sim-1": Waypoint(lat=37.4225, lon=-122.1725, alt_m=0.0),
        "sim-2": Waypoint(lat=37.4225, lon=-122.1725, alt_m=0.0),
    }
    battery = {"sim-1": 100.0, "sim-2": 100.0}

    result = validate_mission_plan(plan, SQUARE_FENCE, constraints, homes, battery)
    assert result.valid is False
    assert any(i.code == "WAYPOINT_OUTSIDE_FENCE" for i in result.issues)


def test_plan_modified_after_approval_is_detected():
    plan = _valid_plan()
    approval = make_approval_record(plan, approved_by="operator-1")

    # The original, unmodified plan still matches its own approval.
    assert plan_matches_approval(plan, approval) is True

    # Mutate a waypoint after approval -- this must be caught, not
    # silently accepted at execution time.
    plan.tasks[0].waypoints[0] = Waypoint(lat=37.5, lon=-122.5, alt_m=20.0)
    assert plan_matches_approval(plan, approval) is False


def test_approval_for_a_different_plan_id_never_matches():
    plan_a = _valid_plan()
    plan_b = _valid_plan()
    plan_b.plan_id = "plan-2"
    approval_a = make_approval_record(plan_a, approved_by="operator-1")
    assert plan_matches_approval(plan_b, approval_a) is False
