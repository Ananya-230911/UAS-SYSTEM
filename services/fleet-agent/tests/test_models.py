from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.core.models import (
    Geofence,
    GeofencePoint,
    MissionPlan,
    MissionRequest,
    Task,
    TaskAssignment,
    Waypoint,
)


def test_valid_waypoint_constructs():
    wp = Waypoint(lat=37.4275, lon=-122.1697, alt_m=20.0)
    assert wp.alt_m == 20.0


@pytest.mark.parametrize(
    "lat,lon,alt_m",
    [
        (91.0, 0.0, 20.0),  # lat out of range
        (0.0, 181.0, 20.0),  # lon out of range
        (0.0, 0.0, -10.0),  # altitude below the schema-level sanity floor
        (0.0, 0.0, 5000.0),  # altitude above the schema-level sanity ceiling
    ],
)
def test_invalid_waypoint_rejected(lat, lon, alt_m):
    with pytest.raises(ValidationError):
        Waypoint(lat=lat, lon=lon, alt_m=alt_m)


def test_geofence_requires_at_least_three_points():
    with pytest.raises(ValidationError):
        Geofence(points=[GeofencePoint(lat=0, lon=0), GeofencePoint(lat=1, lon=1)])

    # Exactly 3 is fine.
    Geofence(
        points=[
            GeofencePoint(lat=0, lon=0),
            GeofencePoint(lat=1, lon=0),
            GeofencePoint(lat=1, lon=1),
        ]
    )


def test_mission_request_requires_at_least_one_vehicle():
    with pytest.raises(ValidationError):
        MissionRequest(goal_text="search the area", vehicle_ids=[])


FIXED_REQUEST_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _sample_plan(plan_id="plan-1", lat_offset=0.0):
    # requested_at must be fixed across calls, not left to its
    # default_factory -- otherwise two calls meant to build "identical
    # content" would never actually match (a test-construction
    # concern, not something content_hash() itself needs to ignore:
    # in real use a MissionPlan's requested_at is set once at proposal
    # time and carried through unchanged to approval/execution).
    task = Task(
        task_id="t1",
        cell_id="cell-1",
        waypoints=[Waypoint(lat=37.4275 + lat_offset, lon=-122.1697, alt_m=20.0)],
    )
    return MissionPlan(
        plan_id=plan_id,
        mission_request=MissionRequest(
            goal_text="search", vehicle_ids=["sim-1"], requested_at=FIXED_REQUEST_TIME
        ),
        tasks=[task],
        assignments=[TaskAssignment(task_id="t1", vehicle_id="sim-1", bid_cost=1.0)],
    )


def test_content_hash_is_deterministic_for_identical_content():
    a = _sample_plan()
    b = _sample_plan()
    assert a.content_hash() == b.content_hash()


def test_content_hash_ignores_plan_id():
    # plan_id is identity, not content -- two plans that differ only in
    # plan_id must hash the same, since the hash exists to detect
    # content tampering, not to double as a plan_id check.
    a = _sample_plan(plan_id="plan-1")
    b = _sample_plan(plan_id="plan-2")
    assert a.content_hash() == b.content_hash()


def test_content_hash_changes_when_a_waypoint_changes():
    a = _sample_plan(lat_offset=0.0)
    b = _sample_plan(lat_offset=0.001)
    assert a.content_hash() != b.content_hash()
