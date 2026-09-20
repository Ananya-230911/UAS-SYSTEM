"""Phase 6b: Remote ID broadcast simulator
(docs/adr/0013-risk-monitoring-and-remote-id.md).

Real drone Remote ID (FAA 14 CFR Part 89 / the ASTM F3411 standard it's
built on) is a radio broadcast (Bluetooth/WiFi) transmitted directly
from the aircraft, receivable by anyone nearby with the right app --
not an HTTP API. This project has no radio layer to broadcast over, so
this module builds a payload with the same *fields* ASTM F3411's Basic
ID + Location + System messages carry, computed from data this project
already has (current telemetry, the vehicle's first-seen position as a
stand-in "operator location", and the Phase 6a emergency_state). It's
exposed as GET /vehicles/{id}/remote_id (see app/main.py) rather than
actually transmitted -- simulating the *content* of a broadcast, not
the broadcast itself. See the ADR for the full list of what that does
and doesn't prove.

Pure function, no DB/session access beyond the plain values passed in,
so it's directly unit-testable (see tests/test_remote_id.py).
"""
from datetime import datetime


def build_remote_id_message(
    vehicle_id: str,
    timestamp: datetime,
    lat: float | None,
    lon: float | None,
    alt_m: float | None,
    relative_alt_m: float | None,
    groundspeed_ms: float | None,
    heading_deg: float | None,
    armed: bool,
    emergency_state: str,
    operator_lat: float | None,
    operator_lon: float | None,
) -> dict:
    """operator_lat/operator_lon stand in for ASTM F3411's Operator
    Location -- this project has no separate ground-control-station
    position, so the vehicle's own first-seen (home/launch) position is
    used, which is documented as a simplification in the ADR rather than
    silently passed off as the genuine article."""
    return {
        "message_version": "sim-1.0",  # not a real ASTM F3411 version -- see module docstring
        "basic_id": {"id_type": "SIMULATED_SERIAL_NUMBER", "uas_id": vehicle_id},
        "location": {
            "timestamp": timestamp.isoformat(),
            "latitude": lat,
            "longitude": lon,
            "geodetic_altitude_m": alt_m,
            "height_above_takeoff_m": relative_alt_m,
            "speed_ms": groundspeed_ms,
            "direction_deg": heading_deg,
        },
        "operator": {"latitude": operator_lat, "longitude": operator_lon},
        "status": {
            "armed": armed,
            # Phase 6a's emergency_state rides along here because "is
            # this aircraft in a declared emergency" is exactly the
            # System Message field ASTM F3411 defines for this --
            # already-tracked data, not a new concept.
            "emergency": emergency_state != "NORMAL",
            "emergency_state": emergency_state,
        },
    }
