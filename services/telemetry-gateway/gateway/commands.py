"""Pure command-building logic, kept separate from the MAVLink I/O so it can
be unit tested without a live connection."""
from dataclasses import dataclass
from typing import List

MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_NAV_TAKEOFF = 22

MODE_NAMES = {0: "STANDBY", 1: "ARMED", 2: "TAKEOFF", 3: "LOITER", 4: "LANDING"}

SUPPORTED_COMMAND_TYPES = {"ARM", "DISARM", "TAKEOFF"}


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
    raise UnsupportedCommand(f"unsupported command type: {command_type!r}")
