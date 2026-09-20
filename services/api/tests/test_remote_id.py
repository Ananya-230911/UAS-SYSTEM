from datetime import datetime, timezone

from app.remote_id import build_remote_id_message


def test_build_remote_id_message_shape():
    msg = build_remote_id_message(
        vehicle_id="sim-1",
        timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        lat=37.4275,
        lon=-122.1697,
        alt_m=50.0,
        relative_alt_m=20.0,
        groundspeed_ms=5.0,
        heading_deg=90.0,
        armed=True,
        emergency_state="NORMAL",
        operator_lat=37.4270,
        operator_lon=-122.1690,
    )
    assert msg["basic_id"] == {"id_type": "SIMULATED_SERIAL_NUMBER", "uas_id": "sim-1"}
    assert msg["location"]["latitude"] == 37.4275
    assert msg["location"]["longitude"] == -122.1697
    assert msg["location"]["timestamp"] == "2026-01-01T12:00:00+00:00"
    assert msg["operator"] == {"latitude": 37.4270, "longitude": -122.1690}
    assert msg["status"] == {"armed": True, "emergency": False, "emergency_state": "NORMAL"}


def test_emergency_state_maps_to_emergency_true():
    msg = build_remote_id_message(
        vehicle_id="sim-1",
        timestamp=datetime.now(timezone.utc),
        lat=None,
        lon=None,
        alt_m=None,
        relative_alt_m=None,
        groundspeed_ms=None,
        heading_deg=None,
        armed=True,
        emergency_state="GEOFENCE_BREACH",
        operator_lat=None,
        operator_lon=None,
    )
    assert msg["status"]["emergency"] is True
    assert msg["status"]["emergency_state"] == "GEOFENCE_BREACH"


def test_handles_missing_position_gracefully():
    # A vehicle with no telemetry yet -- everything position-related is
    # None rather than the call failing.
    msg = build_remote_id_message(
        vehicle_id="never-seen",
        timestamp=datetime.now(timezone.utc),
        lat=None,
        lon=None,
        alt_m=None,
        relative_alt_m=None,
        groundspeed_ms=None,
        heading_deg=None,
        armed=False,
        emergency_state="NORMAL",
        operator_lat=None,
        operator_lon=None,
    )
    assert msg["location"]["latitude"] is None
    assert msg["operator"]["latitude"] is None
