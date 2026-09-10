#!/usr/bin/env python3
"""Lightweight MAVLink SITL stand-in for UAS-SYSTEM Phase 1/2.

This is NOT PX4/ArduPilot SITL. See docs/adr/0001-autopilot-simulator.md for
why: the real PX4 build toolchain (Gazebo, ROS, the firmware source tree)
isn't available in the sandbox this was built in. This module speaks real
MAVLink v2 over UDP and implements enough of the protocol -- HEARTBEAT,
GLOBAL_POSITION_INT, ATTITUDE, SYS_STATUS, GPS_RAW_INT, COMMAND_LONG /
COMMAND_ACK, arm/disarm/takeoff, the mission upload handshake (MISSION_COUNT /
MISSION_REQUEST_INT / MISSION_ITEM_INT / MISSION_ACK, see
docs/adr/0008-mission-protocol.md), waypoint-following, and return-to-launch
-- to exercise the whole Phase 1/2 pipeline end to end. Everything downstream
only depends on the MAVLink wire protocol, so swapping this for real SITL
later does not require touching the gateway, API, or UI.

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
# richer than this; this is intentionally the minimal set Phase 1/2 needs.
# This table and gateway/commands.py's MODE_NAMES are two ends of the same
# wire contract (HEARTBEAT.custom_mode) -- keep them in sync by hand; they
# live in separate processes/services on purpose (see ADR-0006) so there's
# no shared module to enforce it automatically.
MODE_CODES = {
    "STANDBY": 0,
    "ARMED": 1,
    "TAKEOFF": 2,
    "LOITER": 3,
    "LANDING": 4,
    "MISSION": 5,
    "RTL": 6,
}

MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_MISSION_START = 300
MAV_RESULT_ACCEPTED = 0
MAV_RESULT_UNSUPPORTED = 3
MAV_RESULT_FAILED = 4

WAYPOINT_SPEED_MS = 8.0
WAYPOINT_ARRIVAL_RADIUS_M = 3.0
WAYPOINT_ARRIVAL_ALT_TOLERANCE_M = 1.0
RTL_ALTITUDE_M = 20.0
RTL_ARRIVAL_RADIUS_M = 2.0
RTL_LANDED_ALT_TOLERANCE_M = 0.5
CLIMB_DESCENT_RATE_MS = 2.0


class VehicleState:
    """Flight model with three regimes, checked in this priority order:
    RTL > active mission > the original Phase 1 arm-and-loiter behavior.
    Battery drains slowly while armed, in all regimes.
    """

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

        # Phase 2: mission state. mission_waypoints holds the *accepted*
        # mission (list of {"lat", "lon", "alt_m"}); the upload-in-progress
        # buffer is kept separate (see handle_mission_count/_item below) so
        # a partial/failed upload never clobbers a previously accepted one.
        self.mission_waypoints: list = []
        self.mission_active = False
        self.mission_current_seq = 0
        self.rtl_active = False
        self._rtl_over_home = False
        self._mission_upload_buffer: list = []
        self._mission_upload_expected_count = 0

    def _latlon_offset_m(self, lat: float, lon: float) -> tuple:
        """(north_m, east_m) offset of (lat, lon) from home, flat-earth
        approximation -- adequate at the sub-kilometer scale this simulator
        operates at, consistent with the original loiter math below."""
        north_m = math.radians(lat - self.home_lat) * EARTH_RADIUS_M
        east_m = math.radians(lon - self.home_lon) * EARTH_RADIUS_M * math.cos(
            math.radians(self.home_lat)
        )
        return north_m, east_m

    def _offset_m_to_latlon(self, north_m: float, east_m: float) -> tuple:
        lat = self.home_lat + math.degrees(north_m / EARTH_RADIUS_M)
        lon = self.home_lon + math.degrees(
            east_m / (EARTH_RADIUS_M * math.cos(math.radians(self.home_lat)))
        )
        return lat, lon

    def _step_toward_target(
        self, dt: float, target_lat: float, target_lon: float, target_alt_m: float,
        speed_ms: float,
    ) -> tuple:
        """Move at most speed_ms*dt meters toward (target_lat, target_lon),
        and climb/descend at most CLIMB_DESCENT_RATE_MS*dt meters toward
        target_alt_m. Returns (remaining_horizontal_m, remaining_alt_m)
        *after* the move, so callers can decide "have we arrived yet"."""
        cur_n, cur_e = self._latlon_offset_m(self.lat, self.lon)
        tgt_n, tgt_e = self._latlon_offset_m(target_lat, target_lon)
        dn, de = tgt_n - cur_n, tgt_e - cur_e
        horiz_dist = math.hypot(dn, de)
        step = min(speed_ms * dt, horiz_dist)

        if horiz_dist > 1e-6:
            bearing = math.atan2(de, dn)  # radians, 0 = north, clockwise
            new_n = cur_n + step * math.cos(bearing)
            new_e = cur_e + step * math.sin(bearing)
            self.lat, self.lon = self._offset_m_to_latlon(new_n, new_e)
            self.heading_deg = math.degrees(bearing) % 360
            self.yaw = bearing
            self.roll = math.radians(8.0) if step > 0 else 0.0
            self.pitch = math.radians(5.0) if step > 0 else 0.0

        self.groundspeed_ms = (step / dt) if dt > 0 else 0.0

        alt_diff = target_alt_m - self.relative_alt_m
        max_alt_step = CLIMB_DESCENT_RATE_MS * dt
        if abs(alt_diff) > max_alt_step:
            self.relative_alt_m += max_alt_step * (1 if alt_diff > 0 else -1)
        else:
            self.relative_alt_m = target_alt_m
        self.alt_m = self.relative_alt_m

        return horiz_dist - step, abs(target_alt_m - self.relative_alt_m)

    def step(self, dt: float) -> None:
        self._t += dt
        if self.armed and self.battery_pct > 0:
            self.battery_pct = max(0.0, self.battery_pct - 0.01 * dt)
            self.battery_voltage_mv = int(9000 + 36 * self.battery_pct)

        if self.rtl_active:
            self._step_rtl(dt)
        elif self.mission_active:
            self._step_mission(dt)
        elif self.flying:
            self._step_loiter(dt)
        else:
            self.groundspeed_ms = 0.0
            self.mode = "ARMED" if self.armed else "STANDBY"

    def _step_loiter(self, dt: float) -> None:
        """Original Phase 1 behavior, unchanged: climb to target_alt_m,
        then circle home at a fixed radius. Kept as its own method so
        Phase 1's arm/takeoff path and its tests are untouched by Phase 2."""
        if self.relative_alt_m < self.target_alt_m:
            self.relative_alt_m = min(
                self.target_alt_m, self.relative_alt_m + CLIMB_DESCENT_RATE_MS * dt
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

    def _step_mission(self, dt: float) -> None:
        self.mode = "MISSION"
        wp = self.mission_waypoints[self.mission_current_seq]
        remaining_horiz, remaining_alt = self._step_toward_target(
            dt, wp["lat"], wp["lon"], wp["alt_m"], WAYPOINT_SPEED_MS
        )
        if (
            remaining_horiz < WAYPOINT_ARRIVAL_RADIUS_M
            and remaining_alt < WAYPOINT_ARRIVAL_ALT_TOLERANCE_M
        ):
            if self.mission_current_seq + 1 < len(self.mission_waypoints):
                self.mission_current_seq += 1
            else:
                self.mission_active = False
                self.mode = "LOITER"

    def _step_rtl(self, dt: float) -> None:
        if not self._rtl_over_home:
            self.mode = "RTL"
            remaining_horiz, _ = self._step_toward_target(
                dt, self.home_lat, self.home_lon,
                max(self.relative_alt_m, RTL_ALTITUDE_M), WAYPOINT_SPEED_MS,
            )
            if remaining_horiz < RTL_ARRIVAL_RADIUS_M:
                self._rtl_over_home = True
        else:
            self.mode = "LANDING"
            _, remaining_alt = self._step_toward_target(
                dt, self.home_lat, self.home_lon, 0.0, WAYPOINT_SPEED_MS
            )
            if remaining_alt < RTL_LANDED_ALT_TOLERANCE_M:
                self.disarm()

    def start_takeoff(self, target_alt_m: float) -> None:
        self.target_alt_m = target_alt_m
        self.flying = True
        self.mode = "TAKEOFF"

    def start_mission(self) -> None:
        if not self.mission_waypoints:
            raise ValueError("no mission uploaded")
        self.mission_active = True
        self.rtl_active = False
        self._rtl_over_home = False
        self.mission_current_seq = 0
        self.flying = True
        self.mode = "MISSION"

    def start_rtl(self) -> None:
        self.mission_active = False
        self.rtl_active = True
        self._rtl_over_home = False
        self.flying = True
        self.mode = "RTL"

    def arm(self) -> None:
        self.armed = True
        self.mode = "ARMED"

    def disarm(self) -> None:
        self.armed = False
        self.flying = False
        self.mission_active = False
        self.rtl_active = False
        self._rtl_over_home = False
        self.relative_alt_m = 0.0
        self.alt_m = 0.0
        self.target_alt_m = 0.0
        self.mode = "STANDBY"

    def handle_mission_count(self, conn, msg) -> None:
        """MISSION_COUNT received: start (or restart) an upload. Request
        item 0, or ack immediately for an empty mission."""
        self._mission_upload_buffer = [None] * msg.count
        self._mission_upload_expected_count = msg.count
        if msg.count == 0:
            conn.mav.mission_ack_send(
                msg.get_srcSystem(), msg.get_srcComponent(),
                mavutil.mavlink.MAV_MISSION_ACCEPTED,
            )
            self.mission_waypoints = []
            return
        conn.mav.mission_request_int_send(msg.get_srcSystem(), msg.get_srcComponent(), 0)

    def handle_mission_item(self, conn, msg) -> None:
        """MISSION_ITEM_INT received: store it, request the next one, or
        ack once the last expected item has arrived."""
        if 0 <= msg.seq < len(self._mission_upload_buffer):
            self._mission_upload_buffer[msg.seq] = {
                "lat": msg.x / 1e7,
                "lon": msg.y / 1e7,
                "alt_m": msg.z,
            }
        next_seq = msg.seq + 1
        if next_seq < self._mission_upload_expected_count:
            conn.mav.mission_request_int_send(
                msg.get_srcSystem(), msg.get_srcComponent(), next_seq
            )
        else:
            conn.mav.mission_ack_send(
                msg.get_srcSystem(), msg.get_srcComponent(),
                mavutil.mavlink.MAV_MISSION_ACCEPTED,
            )
            self.mission_waypoints = self._mission_upload_buffer
            self._mission_upload_buffer = []


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
    if state.mission_active:
        conn.mav.mission_current_send(state.mission_current_seq)


def handle_command(conn, state: VehicleState, msg) -> None:
    result = MAV_RESULT_ACCEPTED
    if msg.command == MAV_CMD_COMPONENT_ARM_DISARM:
        if msg.param1 >= 0.5:
            state.arm()
        else:
            state.disarm()
    elif msg.command == MAV_CMD_NAV_TAKEOFF:
        if not state.armed:
            result = MAV_RESULT_FAILED
        else:
            target_alt = msg.param7 if msg.param7 > 0 else 20.0
            state.start_takeoff(target_alt)
    elif msg.command == MAV_CMD_MISSION_START:
        if not state.armed:
            result = MAV_RESULT_FAILED
        else:
            try:
                state.start_mission()
            except ValueError:
                result = MAV_RESULT_FAILED
    elif msg.command == MAV_CMD_NAV_RETURN_TO_LAUNCH:
        if not state.armed:
            result = MAV_RESULT_FAILED
        else:
            state.start_rtl()
    else:
        result = MAV_RESULT_UNSUPPORTED

    conn.mav.command_ack_send(msg.command, result)


def prime_connection(conn, state: VehicleState) -> None:
    """Send one HEARTBEAT before the caller ever attempts to receive.

    Required on Windows. pymavlink's 'udpout' mode (see mavutil.mavudp)
    never calls socket.bind() -- it only remembers the destination address
    and sends via sendto(). On POSIX, an unbound UDP socket can still call
    recvfrom(): the kernel lazily binds it to an ephemeral port and a
    non-blocking read with no data just returns EWOULDBLOCK, which
    pymavlink already handles. WinSock has no such lazy-bind-on-recv
    behavior: calling recvfrom() on a UDP socket with no local address
    bound raises WSAEINVAL ("WinError 10022"). Windows *does* auto-bind a
    UDP socket on its first sendto(), so sending one packet here -- before
    run()'s loop ever calls recv_match() -- forces that bind on every
    platform. On Linux/macOS this is a harmless, tolerated duplicate
    heartbeat; on Windows it's what prevents the crash.
    """
    send_heartbeat(conn, state)


def safe_recv_match(conn):
    """conn.recv_match(blocking=False), tolerating a Windows-only UDP reset.

    If a prior sendto() reached a port with no active listener at that
    instant (e.g. the destination process hadn't bound yet, or briefly
    restarted), the OS delivers an ICMP "Port Unreachable" back to the
    *sender's* own socket. POSIX surfaces that on the next recv() as
    ECONNREFUSED, which pymavlink's mavudp.recv() already catches
    internally (see its `except socket.error` clause) -- so on
    Linux/macOS this condition is invisible above pymavlink. WinSock
    surfaces the equivalent condition differently: as WSAECONNRESET
    ("WinError 10054", raised in Python as ConnectionResetError), which is
    not in pymavlink's catch list and propagates straight up. The socket
    itself is not actually broken -- sending/receiving keeps working
    normally afterwards -- so this is safe to treat as "nothing available
    this iteration", identical to a plain non-blocking empty read.
    """
    try:
        return conn.recv_match(blocking=False)
    except ConnectionResetError:
        return None


def dispatch_message(conn, state: VehicleState, msg) -> None:
    mtype = msg.get_type()
    if mtype == "COMMAND_LONG":
        print(f"[sim] COMMAND_LONG command={msg.command} param1={msg.param1} param7={msg.param7}")
        handle_command(conn, state, msg)
    elif mtype == "MISSION_COUNT":
        print(f"[sim] MISSION_COUNT count={msg.count}")
        state.handle_mission_count(conn, msg)
    elif mtype == "MISSION_ITEM_INT":
        state.handle_mission_item(conn, msg)


def run(target_host: str, target_port: int, home_lat: float, home_lon: float) -> None:
    conn = mavutil.mavlink_connection(
        f"udpout:{target_host}:{target_port}",
        source_system=SYSTEM_ID,
        source_component=COMPONENT_ID,
    )
    state = VehicleState(home_lat, home_lon)
    print(f"[sim] streaming MAVLink to {target_host}:{target_port} (sysid={SYSTEM_ID})")

    prime_connection(conn, state)
    last_heartbeat = time.time()
    last_telem = 0.0
    try:
        while True:
            now = time.time()
            msg = safe_recv_match(conn)
            if msg is not None:
                dispatch_message(conn, state, msg)

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
