import pytest

from gateway.commands import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_NAV_TAKEOFF,
    MODE_NAMES,
    UnsupportedCommand,
    build_command,
)


def test_arm_command():
    cmd = build_command("ARM")
    assert cmd.command_id == MAV_CMD_COMPONENT_ARM_DISARM
    assert cmd.params[0] == 1.0


def test_disarm_command():
    cmd = build_command("DISARM")
    assert cmd.command_id == MAV_CMD_COMPONENT_ARM_DISARM
    assert cmd.params[0] == 0.0


def test_takeoff_command_default_altitude():
    cmd = build_command("TAKEOFF")
    assert cmd.command_id == MAV_CMD_NAV_TAKEOFF
    assert cmd.params[6] == 20.0


def test_takeoff_command_custom_altitude():
    cmd = build_command("TAKEOFF", altitude_m=50.0)
    assert cmd.params[6] == 50.0


def test_takeoff_command_rejects_non_positive_altitude():
    cmd = build_command("TAKEOFF", altitude_m=-5.0)
    assert cmd.params[6] == 20.0  # falls back to default


def test_unsupported_command_raises():
    with pytest.raises(UnsupportedCommand):
        build_command("LAND")


def test_mode_names_cover_sim_modes():
    assert MODE_NAMES[0] == "STANDBY"
    assert MODE_NAMES[1] == "ARMED"
    assert MODE_NAMES[2] == "TAKEOFF"
    assert MODE_NAMES[3] == "LOITER"
    assert MODE_NAMES[4] == "LANDING"
