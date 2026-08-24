#!/usr/bin/env python3
"""Lightweight MAVLink SITL stand-in for UAS-SYSTEM Phase 1.

This is NOT PX4/ArduPilot SITL. See docs/adr/0001-autopilot-simulator.md for
why: the real PX4 build toolchain (Gazebo, ROS, the firmware source tree)
isn't available in the sandbox this was built in. This module speaks real
MAVLink v2 over UDP and implements enough of the protocol -- HEARTBEAT,
GLOBAL_POSITION_INT, ATTITUDE, SYS_STATUS, GPS_RAW_INT, COMMAND_LONG /
COMMAND_ACK, arm/disarm, and a simple takeoff-then-loiter flight model -- to
exercise the whole Phase 1 pipeline end to end. Everything downstream only
depends on the MAVLink wire protocol, so swapping this for real SITL later
does not require touching the gateway, API, or UI.

Usage:
    python3 sim.py --target-host 127.0.0.1 --target-port 14550
"""
import argparse
import math
import time

from pymavlink import mavutil

SYSTEM_ID = 1
COMPONENT_ID = mavutil.mavlink.MAV_COMP_ID_AUTOPILOT1

EARTH_RADIUS_M = 6371000.0

# Application-level flight modes. Real PX4/ArduPilot mode tables are far
# richer than this; this is intentionally the minimal set Phase 1 needs.
MODE_CODES = {"STANDBY": 0, "ARMED": 1, "TAKEOFF": 2, "LOITER": 3, "LANDING": 4}

MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_NAV_TAKEOFF = 22
MAV_RESULT_ACCEPTED = 0


class VehicleState:
    """Minimal flight model: arm, climb to a target altitude, then loiter
    in a circle around home. Battery drains slowly while armed."""

    def __init__(self, home_lat: float, home_lon: float):
        self.home_lat = home_lat
        self.home_lon = home_lon
        self.lat = home_lat
        self.lon = home_lon
        self.alt_m = 0.0
        self.relative_alt_m = 0.0
        self.armed = False
        self.mode = "STANDBY"
        self.battery_pct = 100.0
        self.battery_voltage_mv = 12600
        self.heading_deg = 0.0
        self.groundspeed_ms = 0.0
        self.roll = 0.0
        self.pitch = 0.0
        self.yaw = 0.0
        self.target_alt_m = 0.0
        self.flying = False
        self._t = 0.0

    def step(self, dt: float) -> None:
        self._t += dt
        if self.armed and self.battery_pct > 0:
            self.battery_pct = max(0.0, self.battery_pct - 0.01 * dt)
            self.battery_voltage_mv = int(9000 + 36 * self.battery_pct)

        if self.flying:
            climb_rate_ms = 2.0
            if self.relative_alt_m < self.target_alt_m:
                self.relative_alt_m = min(
                    self.target_alt_m, self.relative_alt_m + climb_rate_ms * dt
                )
                self.mode = "TAKEOFF"
            else:
                self.mode = "LOITER"
            self.alt_m = self.relative_alt_m

            radius_m = 40.0
            angular_speed = 0.15
            angle = self._t * angular_speed
            self.groundspeed_ms = radius_m * angular_speed
            self.heading_deg = (math.degrees(angle) + 90) % 360
            self.yaw = math.radians(self.heading_deg)
            self.roll = math.radians(15 * math.sin(angle))
            self.pitch = math.radians(5)

            dlat = (radius_m * math.cos(angle)) / EARTH_RADIUS_M
            dlon = (radius_m * math.sin(angle)) / (
                EARTH_RADIUS_M * math.cos(math.radians(self.home_lat))
            )
            self.lat = self.home_lat + math.degrees(dlat)
            self.lon = self.home_lon + math.degrees(dlon)
        else:
            self.groundspeed_ms = 0.0
            self.mode = "ARMED" if self.armed else "STANDBY"

    def start_takeoff(self, target_alt_m: float) -> None:
        self.target_alt_m = target_alt_m
        self.flying = True
        self.mode = "TAKEOFF"

    def arm(self) -> None:
        self.armed = True
        self.mode = "ARMED"

    def disarm(self) -> None:
        self.armed = False
        self.flying = False
        self.relative_alt_m = 0.0
        self.alt_m = 0.0
        self.target_alt_m = 0.0
        self.mode = "STANDBY"


def send_heartbeat(conn, state: VehicleState) -> None:
    base_mode = mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
    if state.armed:
        base_mode |= mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
    conn.mav.heartbeat_send(
        mavutil.mavlink.MAV_TYPE_QUADROTOR,
        mavutil.mavlink.MAV_AUTOPILOT_GENERIC,
        base_mode,
        MODE_CODES.get(state.mode, 0),
        mavutil.mavlink.MAV_STATE_ACTIVE if state.armed else mavutil.mavlink.MAV_STATE_STANDBY,
    )


def send_telemetry(conn, state: VehicleState) -> None:
    now_ms = int(time.time() * 1000) & 0xFFFFFFFF
    conn.mav.global_position_int_send(
        now_ms,
        int(state.lat * 1e7),
        int(state.lon * 1e7),
        int(state.alt_m * 1000),
        int(state.relative_alt_m * 1000),
        0, 0, 0,
        int(state.heading_deg * 100),
    )
    conn.mav.attitude_send(now_ms, state.roll, state.pitch, state.yaw, 0, 0, 0)
    conn.mav.sys_status_send(
        0, 0, 0, 0,
        state.battery_voltage_mv, -1,
        int(state.battery_pct),
        0, 0, 0, 0, 0, 0,
    )
    conn.mav.gps_raw_int_send(
        int(time.time() * 1e6) & 0xFFFFFFFFFFFFFFFF,
        3,  # fix_type: 3D fix
        int(state.lat * 1e7),
        int(state.lon * 1e7),
        int(state.alt_m * 1000),
        100, 100,
        int(state.groundspeed_ms * 100),
        int(state.heading_deg * 100),
        10,
    )


def handle_command(conn, state: VehicleState, msg) -> None:
    result = MAV_RESULT_ACCEPTED
    if msg.command == MAV_CMD_COMPONENT_ARM_DISARM:
        if msg.param1 >= 0.5:
            state.arm()
        else:
            state.disarm()
    elif msg.command == MAV_CMD_NAV_TAKEOFF:
        if not state.armed:
            result = 4  # MAV_RESULT_FAILED (not armed)
        else:
            target_alt = msg.param7 if msg.param7 > 0 else 20.0
            state.start_takeoff(target_alt)
    else:
        result = 3  # MAV_RESULT_UNSUPPORTED

    conn.mav.command_ack_send(msg.command, result)


def run(target_host: str, target_port: int, home_lat: float, home_lon: float) -> None:
    conn = mavutil.mavlink_connection(
        f"udpout:{target_host}:{target_port}",
        source_system=SYSTEM_ID,
        source_component=COMPONENT_ID,
    )
    state = VehicleState(home_lat, home_lon)
    print(f"[sim] streaming MAVLink to {target_host}:{target_port} (sysid={SYSTEM_ID})")

    last_heartbeat = 0.0
    last_telem = 0.0
    try:
        while True:
            now = time.time()
            msg = conn.recv_match(blocking=False)
            if msg is not None and msg.get_type() == "COMMAND_LONG":
                print(f"[sim] COMMAND_LONG command={msg.command} param1={msg.param1} param7={msg.param7}")
                handle_command(conn, state, msg)

            if now - last_heartbeat >= 1.0:
                send_heartbeat(conn, state)
                last_heartbeat = now

            if now - last_telem >= 0.2:
                dt = (now - last_telem) if last_telem else 0.2
                state.step(dt)
                send_telemetry(conn, state)
                last_telem = now

            time.sleep(0.01)
    except KeyboardInterrupt:
        print("[sim] stopping")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=14550)
    parser.add_argument("--home-lat", type=float, default=37.4275)
    parser.add_argument("--home-lon", type=float, default=-122.1697)
    args = parser.parse_args()
    run(args.target_host, args.target_port, args.home_lat, args.home_lon)


if __name__ == "__main__":
    main()
