from types import SimpleNamespace

from pymavlink import mavutil

from gateway.mavlink_client import MavlinkGatewayClient


def _msg(type_name: str, **fields):
    ns = SimpleNamespace(**fields)
    ns.get_type = lambda: type_name
    return ns


def test_heartbeat_updates_armed_and_mode():
    client = MavlinkGatewayClient()
    client._handle_message(
        _msg(
            "HEARTBEAT",
            base_mode=mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED,
            custom_mode=3,
        )
    )
    sample = client.snapshot()
    assert sample["armed"] is True
    assert sample["flight_mode"] == "LOITER"


def test_global_position_int_updates_position():
    client = MavlinkGatewayClient()
    client._handle_message(
        _msg(
            "GLOBAL_POSITION_INT",
            lat=374275000,
            lon=-1221697000,
            alt=20000,
            relative_alt=20000,
            hdg=9000,
        )
    )
    sample = client.snapshot()
    assert sample["lat"] == 37.4275
    assert sample["lon"] == -122.1697
    assert sample["alt_m"] == 20.0
    assert sample["relative_alt_m"] == 20.0
    assert sample["heading_deg"] == 90.0


def test_sys_status_updates_battery():
    client = MavlinkGatewayClient()
    client._handle_message(
        _msg("SYS_STATUS", battery_remaining=77, voltage_battery=11800)
    )
    sample = client.snapshot()
    assert sample["battery_pct"] == 77
    assert sample["battery_voltage_mv"] == 11800


def test_snapshot_returns_none_when_nothing_changed():
    client = MavlinkGatewayClient()
    assert client.snapshot() is None


def test_command_ack_resolves_pending_command():
    client = MavlinkGatewayClient()
    import threading

    event = threading.Event()
    box = {}
    with client._pending_lock:
        client._pending_commands[400] = (event, box)

    client._handle_message(_msg("COMMAND_ACK", command=400, result=0))

    assert event.wait(timeout=1)
    assert box["result"] == 0
