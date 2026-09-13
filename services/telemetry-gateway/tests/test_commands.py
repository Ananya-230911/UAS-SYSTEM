import pytest

from gateway.commands import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_MISSION_START,
    MAV_CMD_NAV_RETURN_TO_LAUNCH,
    MAV_CMD_NAV_TAKEOFF,
    MODE_NAMES,
    UnsupportedCommand,
    build_command,
    mission_upload_timeout_for,
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
    assert MODE_NAMES[5] == "MISSION"
    assert MODE_NAMES[6] == "RTL"


def test_rtl_command():
    cmd = build_command("RTL")
    assert cmd.command_id == MAV_CMD_NAV_RETURN_TO_LAUNCH
    assert cmd.params == []


def test_mission_start_command():
    cmd = build_command("MISSION_START")
    assert cmd.command_id == MAV_CMD_MISSION_START
    assert cmd.params == [0.0, 0.0]


def test_mission_upload_timeout_has_a_floor_for_small_missions():
    assert mission_upload_timeout_for(0) == 10.0
    assert mission_upload_timeout_for(1) == 10.0


def test_mission_upload_timeout_scales_with_waypoint_count():
    assert mission_upload_timeout_for(10) == 20.0
    assert mission_upload_timeout_for(20) == 40.0
