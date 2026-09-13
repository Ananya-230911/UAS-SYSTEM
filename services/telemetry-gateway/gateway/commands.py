"""Pure command-building logic, kept separate from the MAVLink I/O so it can
be unit tested without a live connection."""
from dataclasses import dataclass
from typing import List

MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_MISSION_START = 300

# Mirrors simulation/sitl/sim.py's MODE_CODES -- see that file's comment on
# why these live in two places instead of one shared module.
MODE_NAMES = {
    0: "STANDBY",
    1: "ARMED",
    2: "TAKEOFF",
    3: "LOITER",
    4: "LANDING",
    5: "MISSION",
    6: "RTL",
}

SUPPORTED_COMMAND_TYPES = {"ARM", "DISARM", "TAKEOFF", "RTL", "MISSION_START"}


class UnsupportedCommand(ValueError):
    pass


@dataclass
class MavCommand:
    command_id: int
    params: List[float]


def build_command(command_type: str, altitude_m: float | None = None) -> MavCommand:
    """Translate an application-level command type into a MAV_CMD id + params."""
    if command_type == "ARM":
        return MavCommand(MAV_CMD_COMPONENT_ARM_DISARM, [1.0])
    if command_type == "DISARM":
        return MavCommand(MAV_CMD_COMPONENT_ARM_DISARM, [0.0])
    if command_type == "TAKEOFF":
        alt = altitude_m if altitude_m and altitude_m > 0 else 20.0
        # MAV_CMD_NAV_TAKEOFF params 1-6 unused here, param7 = altitude
        return MavCommand(MAV_CMD_NAV_TAKEOFF, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, alt])
    if command_type == "RTL":
        return MavCommand(MAV_CMD_NAV_RETURN_TO_LAUNCH, [])
    if command_type == "MISSION_START":
        # param1/param2 = first/last mission item; 0/0 means "run the whole
        # uploaded mission from the start."
        return MavCommand(MAV_CMD_MISSION_START, [0.0, 0.0])
    raise UnsupportedCommand(f"unsupported command type: {command_type!r}")


def mission_upload_timeout_for(waypoint_count: int) -> float:
    """How long to allow a mission upload handshake to take, scaled by
    waypoint count -- each waypoint is its own MISSION_REQUEST_INT /
    MISSION_ITEM_INT round trip (see docs/adr/0008-mission-protocol.md), so
    a fixed timeout that's fine for 2 waypoints can be too tight for 10+,
    especially with real network latency (observed on Windows during
    Phase 2 testing). services/api's mission_upload_timeout_s must stay
    comfortably above whatever this returns for realistic mission sizes."""
    return max(10.0, 2.0 * waypoint_count)
