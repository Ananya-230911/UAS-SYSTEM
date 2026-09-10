"""Owns the single MAVLink UDP connection to the simulated/real vehicle.

pymavlink's socket API is blocking, not asyncio-friendly, so this runs its
own receive loop on a background thread and exposes thread-safe methods that
FastAPI's (sync) route handlers call from worker threads.
"""
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from pymavlink import mavutil

from .commands import MODE_NAMES
from .config import settings


class CommandTimeout(Exception):
    pass


class MissionUploadTimeout(Exception):
    pass


class MissionUploadRejected(Exception):
    pass


@dataclass
class VehicleSample:
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_m: Optional[float] = None
    relative_alt_m: Optional[float] = None
    heading_deg: Optional[float] = None
    groundspeed_ms: Optional[float] = None
    roll: Optional[float] = None
    pitch: Optional[float] = None
    yaw: Optional[float] = None
    battery_pct: Optional[float] = None
    battery_voltage_mv: Optional[float] = None
    gps_fix_type: Optional[int] = None
    satellites_visible: Optional[int] = None
    armed: bool = False
    flight_mode: str = "UNKNOWN"
    current_waypoint_seq: Optional[int] = None
    updated: bool = field(default=False, repr=False)


class MavlinkGatewayClient:
    def __init__(self) -> None:
        self._conn = None
        self._sample = VehicleSample()
        self._sample_lock = threading.Lock()
        self._pending_commands: dict = {}
        self._pending_lock = threading.Lock()
        self._mission_request_queue: "queue.Queue[int]" = queue.Queue()
        self._mission_ack_event = threading.Event()
        self._mission_ack_box: dict = {}
        self._stop = threading.Event()
        self._recv_thread: Optional[threading.Thread] = None
        self._poster_thread: Optional[threading.Thread] = None
        self._connected_event = threading.Event()

    def start(self) -> None:
        self._conn = mavutil.mavlink_connection(
            f"udpin:{settings.mavlink_bind_host}:{settings.mavlink_bind_port}",
            source_system=255,
            source_component=0,
        )
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        self._poster_thread = threading.Thread(target=self._post_loop, daemon=True)
        self._poster_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_connected(self) -> bool:
        return self._connected_event.is_set()

    def wait_for_heartbeat(self, timeout: Optional[float] = None) -> bool:
        return self._connected_event.wait(timeout=timeout)

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            msg = self._safe_recv_match(timeout=0.5)
            if msg is None:
                continue
            self._handle_message(msg)

    def _safe_recv_match(self, timeout: float):
        """self._conn.recv_match(...), tolerating a Windows-only UDP reset.

        Same condition as simulation/sitl/sim.py's safe_recv_match(): if
        the simulator (or a real vehicle) restarts or is briefly
        unreachable while this gateway is running, an ICMP Port
        Unreachable can hit this socket. POSIX surfaces that as
        ECONNREFUSED, already swallowed inside pymavlink's mavudp.recv();
        Windows surfaces it as WSAECONNRESET (ConnectionResetError), which
        is not caught there. Left unhandled this would silently kill this
        background thread (a daemon thread dying raises no visible error),
        permanently stopping telemetry ingestion. Treating it as "nothing
        available this iteration" matches a plain read timeout.
        """
        try:
            return self._conn.recv_match(blocking=True, timeout=timeout)
        except ConnectionResetError:
            return None

    def _handle_message(self, msg) -> None:
        mtype = msg.get_type()
        if mtype == "COMMAND_ACK":
            self._resolve_command(msg.command, msg.result)
            return
        if mtype == "MISSION_REQUEST_INT":
            self._mission_request_queue.put(msg.seq)
            return
        if mtype == "MISSION_ACK":
            self._mission_ack_box["type"] = msg.type
            self._mission_ack_event.set()
            return

        with self._sample_lock:
            if mtype == "HEARTBEAT":
                self._connected_event.set()
                self._sample.armed = bool(
                    msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                )
                self._sample.flight_mode = MODE_NAMES.get(
                    msg.custom_mode, f"MODE_{msg.custom_mode}"
                )
                self._sample.updated = True
            elif mtype == "GLOBAL_POSITION_INT":
                self._sample.lat = msg.lat / 1e7
                self._sample.lon = msg.lon / 1e7
                self._sample.alt_m = msg.alt / 1000.0
                self._sample.relative_alt_m = msg.relative_alt / 1000.0
                self._sample.heading_deg = msg.hdg / 100.0 if msg.hdg != 65535 else None
                self._sample.updated = True
            elif mtype == "ATTITUDE":
                self._sample.roll = msg.roll
                self._sample.pitch = msg.pitch
                self._sample.yaw = msg.yaw
                self._sample.updated = True
            elif mtype == "SYS_STATUS":
                self._sample.battery_pct = (
                    msg.battery_remaining if msg.battery_remaining >= 0 else None
                )
                self._sample.battery_voltage_mv = (
                    msg.voltage_battery if msg.voltage_battery != 65535 else None
                )
                self._sample.updated = True
            elif mtype == "GPS_RAW_INT":
                self._sample.gps_fix_type = msg.fix_type
                self._sample.satellites_visible = msg.satellites_visible
                if msg.vel != 65535:
                    self._sample.groundspeed_ms = msg.vel / 100.0
                self._sample.updated = True
            elif mtype == "MISSION_CURRENT":
                self._sample.current_waypoint_seq = msg.seq
                self._sample.updated = True

    def _resolve_command(self, command_id: int, result: int) -> None:
        with self._pending_lock:
            entry = self._pending_commands.get(command_id)
            if entry is not None:
                event, box = entry
                box["result"] = result
                event.set()

    def send_command(self, command_id: int, params: list, timeout: float = 5.0) -> int:
        """Send a MAV_CMD_* COMMAND_LONG and block for its COMMAND_ACK.
        Returns the MAV_RESULT code, or raises CommandTimeout."""
        event = threading.Event()
        box: dict = {}
        with self._pending_lock:
            self._pending_commands[command_id] = (event, box)
        padded = list(params) + [0.0] * (7 - len(params))
        self._conn.mav.command_long_send(
            self._conn.target_system or 1,
            self._conn.target_component or 1,
            command_id,
            0,
            *padded,
        )
        acked = event.wait(timeout=timeout)
        with self._pending_lock:
            self._pending_commands.pop(command_id, None)
        if not acked:
            raise CommandTimeout(f"no COMMAND_ACK for command {command_id} within {timeout}s")
        return box["result"]

    def upload_mission(self, waypoints: list, timeout: float = 10.0) -> None:
        """Upload a mission via the standard MAVLink handshake (see
        docs/adr/0008-mission-protocol.md): send MISSION_COUNT, answer each
        MISSION_REQUEST_INT with the matching MISSION_ITEM_INT in order,
        then wait for the final MISSION_ACK. Raises MissionUploadTimeout or
        MissionUploadRejected on failure; returns normally on acceptance.
        """
        self._mission_ack_event.clear()
        self._mission_ack_box.clear()
        while not self._mission_request_queue.empty():
            self._mission_request_queue.get_nowait()

        target_system = self._conn.target_system or 1
        target_component = self._conn.target_component or 1
        deadline = time.time() + timeout

        self._conn.mav.mission_count_send(target_system, target_component, len(waypoints))
        served = 0
        while served < len(waypoints):
            remaining = deadline - time.time()
            if remaining <= 0:
                raise MissionUploadTimeout(
                    f"vehicle did not request all {len(waypoints)} mission items in time"
                )
            try:
                seq = self._mission_request_queue.get(timeout=remaining)
            except queue.Empty:
                raise MissionUploadTimeout(
                    f"vehicle did not request all {len(waypoints)} mission items in time"
                )
            wp = waypoints[seq]
            self._conn.mav.mission_item_int_send(
                target_system, target_component, seq,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                1 if seq == 0 else 0,  # current
                1,  # autocontinue
                0.0, 0.0, 0.0, 0.0,
                int(wp["lat"] * 1e7), int(wp["lon"] * 1e7), float(wp["alt_m"]),
            )
            served += 1

        acked = self._mission_ack_event.wait(timeout=max(0.0, deadline - time.time()))
        if not acked:
            raise MissionUploadTimeout("no MISSION_ACK received within timeout")
        if self._mission_ack_box.get("type") != mavutil.mavlink.MAV_MISSION_ACCEPTED:
            raise MissionUploadRejected(
                f"vehicle rejected mission: MAV_MISSION_RESULT={self._mission_ack_box.get('type')}"
            )

    def snapshot(self) -> Optional[dict]:
        """Return the latest sample and clear the dirty flag, or None if
        nothing has changed since the last snapshot."""
        with self._sample_lock:
            if not self._sample.updated:
                return None
            data = {
                k: v for k, v in self._sample.__dict__.items() if k != "updated"
            }
            self._sample.updated = False
            return data

    def _post_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(settings.telemetry_post_interval_s)
            sample = self.snapshot()
            if sample is None:
                continue
            payload = {"vehicle_id": settings.vehicle_id, **sample}
            try:
                requests.post(
                    f"{settings.api_base_url}/internal/telemetry",
                    json=payload,
                    headers={"X-Internal-Token": settings.internal_token},
                    timeout=3,
                )
            except requests.RequestException as exc:
                print(f"[gateway] failed to post telemetry: {exc}")


gateway_client = MavlinkGatewayClient()
