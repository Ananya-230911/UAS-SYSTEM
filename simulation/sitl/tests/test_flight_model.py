"""Coverage for the Phase 2 waypoint-following/RTL flight math
(VehicleState._step_toward_target, _step_mission, _step_rtl), and a
regression check that Phase 1's arm/takeoff/loiter path is untouched."""
import math

import pytest

from sim import VehicleState


def test_step_toward_target_moves_at_the_requested_speed():
    state = VehicleState(home_lat=0.0, home_lon=0.0)
    # Target due north, far enough away that one step doesn't arrive.
    target_lat = 0.001  # roughly 111m north of home
    remaining_horiz, remaining_alt = state._step_toward_target(
        dt=1.0, target_lat=target_lat, target_lon=0.0, target_alt_m=0.0, speed_ms=8.0
    )
    assert state.groundspeed_ms == 8.0
    assert 0 <= state.heading_deg < 5 or state.heading_deg > 355  # ~north
    assert remaining_horiz < 111 - 7  # moved roughly 8m closer


def test_step_toward_target_climbs_toward_target_altitude():
    state = VehicleState(home_lat=0.0, home_lon=0.0)
    state._step_toward_target(
        dt=1.0, target_lat=0.0, target_lon=0.0, target_alt_m=20.0, speed_ms=8.0
    )
    assert state.relative_alt_m == 2.0  # CLIMB_DESCENT_RATE_MS * dt
    assert state.alt_m == 2.0


def test_step_toward_target_snaps_to_target_when_within_one_step():
    state = VehicleState(home_lat=0.0, home_lon=0.0)
    state.relative_alt_m = 19.0
    remaining_horiz, remaining_alt = state._step_toward_target(
        dt=1.0, target_lat=0.0, target_lon=0.0, target_alt_m=20.0, speed_ms=8.0
    )
    assert state.relative_alt_m == 20.0
    assert remaining_alt == 0.0
    assert remaining_horiz == 0.0  # already at (0,0)


def test_mission_advances_to_next_waypoint_on_arrival():
    state = VehicleState(home_lat=0.0, home_lon=0.0)
    state.mission_waypoints = [
        {"lat": 0.0, "lon": 0.0, "alt_m": 0.0},  # already "there"
        {"lat": 0.01, "lon": 0.01, "alt_m": 20.0},
    ]
    state.arm()
    state.start_mission()

    state.step(dt=1.0)

    assert state.mission_current_seq == 1
    assert state.mission_active is True


def test_mission_completes_and_loiters_after_last_waypoint():
    state = VehicleState(home_lat=0.0, home_lon=0.0)
    state.mission_waypoints = [{"lat": 0.0, "lon": 0.0, "alt_m": 0.0}]
    state.arm()
    state.start_mission()

    state.step(dt=1.0)

    assert state.mission_active is False
    assert state.mode == "LOITER"


def test_rtl_flies_home_then_lands_and_disarms():
    state = VehicleState(home_lat=0.0, home_lon=0.0)
    state.arm()
    state.lat, state.lon = 0.001, 0.001  # away from home
    state.relative_alt_m = 20.0
    state.start_rtl()

    # Run enough steps to get home, then land. Bounded loop so a bug can't
    # hang the test suite.
    for _ in range(200):
        state.step(dt=0.5)
        if not state.armed:
            break

    assert state.armed is False
    assert state.rtl_active is False
    assert state.relative_alt_m == 0.0


def test_phase1_loiter_path_is_unaffected_by_phase2_additions():
    # Regression guard: plain arm + takeoff (no mission, no RTL) must still
    # behave exactly as in Phase 1 -- climb, then circle home.
    state = VehicleState(home_lat=37.4275, home_lon=-122.1697)
    state.arm()
    state.start_takeoff(20.0)

    # 50 steps of dt=0.2 is exactly the 10s climb time (20m / 2m/s); mode
    # reflects the pre-step state so it needs one more step past that to
    # settle at LOITER once altitude has clamped to the target.
    for _ in range(60):
        state.step(dt=0.2)

    assert state.relative_alt_m == pytest.approx(20.0)
    assert state.mode == "LOITER"
    dist_from_home = math.hypot(
        *state._latlon_offset_m(state.lat, state.lon)
    )
    assert 30 < dist_from_home < 50  # circling at ~40m radius
