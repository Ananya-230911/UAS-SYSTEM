"""Coverage for MavlinkGatewayClient.upload_mission() -- the gateway side of
the mission handshake described in docs/adr/0008-mission-protocol.md."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pymavlink import mavutil

from gateway.mavlink_client import (
    MavlinkGatewayClient,
    MissionUploadRejected,
    MissionUploadTimeout,
)


def _msg(type_name: str, **fields):
    ns = SimpleNamespace(**fields)
    ns.get_type = lambda: type_name
    return ns


def _make_client() -> MavlinkGatewayClient:
    client = MavlinkGatewayClient()
    client._conn = MagicMock()
    client._conn.target_system = 1
    client._conn.target_component = 1
    return client


def _wait_until(predicate, timeout=2.0):
    """Spin-wait for an upload_mission() call running on another thread to
    reach a checkpoint, avoiding a race where the simulated vehicle's
    responses arrive before upload_mission() has cleared its ack event /
    sent MISSION_COUNT."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for upload_mission() to reach checkpoint")


def test_upload_mission_sends_count_then_each_requested_item():
    client = _make_client()
    waypoints = [
        {"lat": 1.0, "lon": 2.0, "alt_m": 10.0},
        {"lat": 3.0, "lon": 4.0, "alt_m": 20.0},
    ]

    def simulate_vehicle():
        _wait_until(lambda: client._conn.mav.mission_count_send.called)
        client._handle_message(_msg("MISSION_REQUEST_INT", seq=0))
        _wait_until(lambda: client._conn.mav.mission_item_int_send.call_count >= 1)
        client._handle_message(_msg("MISSION_REQUEST_INT", seq=1))
        _wait_until(lambda: client._conn.mav.mission_item_int_send.call_count >= 2)
        client._handle_message(
            _msg("MISSION_ACK", type=mavutil.mavlink.MAV_MISSION_ACCEPTED)
        )

    thread = threading.Thread(target=simulate_vehicle)
    thread.start()
    client.upload_mission(waypoints, timeout=2.0)
    thread.join()

    client._conn.mav.mission_count_send.assert_called_once_with(1, 1, 2)
    assert client._conn.mav.mission_item_int_send.call_count == 2

    first_call_args = client._conn.mav.mission_item_int_send.call_args_list[0][0]
    assert first_call_args[2] == 0  # seq
    assert first_call_args[4] == mavutil.mavlink.MAV_CMD_NAV_WAYPOINT
    assert first_call_args[5] == 1  # current == True for seq 0
    assert first_call_args[11] == int(1.0 * 1e7)  # x = lat * 1e7
    assert first_call_args[12] == int(2.0 * 1e7)  # y = lon * 1e7
    assert first_call_args[13] == 10.0  # z = alt_m

    second_call_args = client._conn.mav.mission_item_int_send.call_args_list[1][0]
    assert second_call_args[2] == 1
    assert second_call_args[5] == 0  # current == False for seq > 0


def test_upload_mission_raises_on_timeout_when_vehicle_never_requests():
    client = _make_client()
    waypoints = [{"lat": 1.0, "lon": 2.0, "alt_m": 10.0}]

    with pytest.raises(MissionUploadTimeout):
        client.upload_mission(waypoints, timeout=0.2)


def test_upload_mission_raises_on_rejection():
    client = _make_client()
    waypoints = [{"lat": 1.0, "lon": 2.0, "alt_m": 10.0}]

    def simulate_vehicle():
        _wait_until(lambda: client._conn.mav.mission_count_send.called)
        client._handle_message(_msg("MISSION_REQUEST_INT", seq=0))
        _wait_until(lambda: client._conn.mav.mission_item_int_send.call_count >= 1)
        client._handle_message(_msg("MISSION_ACK", type=mavutil.mavlink.MAV_MISSION_ERROR))

    thread = threading.Thread(target=simulate_vehicle)
    thread.start()
    with pytest.raises(MissionUploadRejected):
        client.upload_mission(waypoints, timeout=2.0)
    thread.join()


def test_upload_mission_with_empty_list_just_waits_for_ack():
    client = _make_client()

    def simulate_vehicle():
        _wait_until(lambda: client._conn.mav.mission_count_send.called)
        client._handle_message(_msg("MISSION_ACK", type=mavutil.mavlink.MAV_MISSION_ACCEPTED))

    thread = threading.Thread(target=simulate_vehicle)
    thread.start()
    client.upload_mission([], timeout=2.0)
    thread.join()

    client._conn.mav.mission_count_send.assert_called_once_with(1, 1, 0)
    client._conn.mav.mission_item_int_send.assert_not_called()
