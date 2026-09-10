"""Coverage for the Phase 2 mission upload handshake (ADR-0008) and the
start_mission()/start_rtl()/disarm() state transitions."""
from unittest.mock import MagicMock

import pytest
from pymavlink import mavutil

from sim import VehicleState


def _mission_count_msg(count, src_system=255, src_component=0):
    msg = MagicMock()
    msg.count = count
    msg.get_srcSystem.return_value = src_system
    msg.get_srcComponent.return_value = src_component
    return msg


def _mission_item_msg(seq, lat, lon, alt_m, src_system=255, src_component=0):
    msg = MagicMock()
    msg.seq = seq
    msg.x = int(lat * 1e7)
    msg.y = int(lon * 1e7)
    msg.z = alt_m
    msg.get_srcSystem.return_value = src_system
    msg.get_srcComponent.return_value = src_component
    return msg


def test_mission_upload_handshake_requests_items_in_order_and_acks():
    conn = MagicMock()
    state = VehicleState(home_lat=0.0, home_lon=0.0)

    state.handle_mission_count(conn, _mission_count_msg(2))
    conn.mav.mission_request_int_send.assert_called_once_with(255, 0, 0)
    assert state.mission_waypoints == []  # not yet accepted mid-upload

    conn.mav.mission_request_int_send.reset_mock()
    state.handle_mission_item(conn, _mission_item_msg(0, 1.0, 2.0, 10.0))
    conn.mav.mission_request_int_send.assert_called_once_with(255, 0, 1)
    conn.mav.mission_ack_send.assert_not_called()

    state.handle_mission_item(conn, _mission_item_msg(1, 3.0, 4.0, 20.0))
    conn.mav.mission_ack_send.assert_called_once_with(
        255, 0, mavutil.mavlink.MAV_MISSION_ACCEPTED
    )
    assert state.mission_waypoints == [
        {"lat": 1.0, "lon": 2.0, "alt_m": 10.0},
        {"lat": 3.0, "lon": 4.0, "alt_m": 20.0},
    ]


def test_empty_mission_count_acks_immediately_with_no_requests():
    conn = MagicMock()
    state = VehicleState(home_lat=0.0, home_lon=0.0)

    state.handle_mission_count(conn, _mission_count_msg(0))

    conn.mav.mission_request_int_send.assert_not_called()
    conn.mav.mission_ack_send.assert_called_once_with(
        255, 0, mavutil.mavlink.MAV_MISSION_ACCEPTED
    )
    assert state.mission_waypoints == []


def test_partial_upload_does_not_clobber_a_previously_accepted_mission():
    conn = MagicMock()
    state = VehicleState(home_lat=0.0, home_lon=0.0)
    state.mission_waypoints = [{"lat": 9.0, "lon": 9.0, "alt_m": 9.0}]

    # A new upload starts but hasn't finished yet.
    state.handle_mission_count(conn, _mission_count_msg(2))

    assert state.mission_waypoints == [{"lat": 9.0, "lon": 9.0, "alt_m": 9.0}]


def test_start_mission_requires_an_uploaded_mission():
    state = VehicleState(home_lat=0.0, home_lon=0.0)
    with pytest.raises(ValueError):
        state.start_mission()


def test_start_mission_sets_active_state():
    state = VehicleState(home_lat=0.0, home_lon=0.0)
    state.mission_waypoints = [{"lat": 1.0, "lon": 1.0, "alt_m": 10.0}]

    state.start_mission()

    assert state.mission_active is True
    assert state.rtl_active is False
    assert state.mission_current_seq == 0
    assert state.mode == "MISSION"


def test_start_rtl_cancels_an_active_mission():
    state = VehicleState(home_lat=0.0, home_lon=0.0)
    state.mission_waypoints = [{"lat": 1.0, "lon": 1.0, "alt_m": 10.0}]
    state.start_mission()

    state.start_rtl()

    assert state.rtl_active is True
    assert state.mission_active is False
    assert state.mode == "RTL"


def test_disarm_clears_mission_and_rtl_state():
    state = VehicleState(home_lat=0.0, home_lon=0.0)
    state.mission_waypoints = [{"lat": 1.0, "lon": 1.0, "alt_m": 10.0}]
    state.start_mission()
    state.arm()

    state.disarm()

    assert state.mission_active is False
    assert state.rtl_active is False
    assert state.armed is False
    assert state.mode == "STANDBY"
