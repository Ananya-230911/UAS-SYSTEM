#!/usr/bin/env python3
"""End-to-end Phase 1/2/3 smoke test.

Starts api + telemetry-gateway + sitl as subprocesses, waits for telemetry to
flow, arms the simulated vehicle, commands takeoff, and asserts altitude
increases (Phase 1). Then uploads and starts a two-waypoint mission and
asserts the vehicle actually flies it (current_waypoint_seq advances), and
commands RTL and asserts the vehicle flies home and disarms itself (Phase 2,
see docs/adr/0008-mission-protocol.md). Then brings up a second vehicle
(its own gateway + simulator) and asserts a command sent to it reaches its
own gateway and only its own gateway -- proving the multi-vehicle command
routing added in Phase 3 actually works, not just that two vehicles can
send telemetry (see docs/adr/0009-multi-vehicle-fleet.md). Then sets a
geofence around sim-1's home and confirms a mission with a waypoint
outside it is rejected while one fully inside still uploads normally
(Phase 6a, see docs/adr/0012-geofencing-failsafe.md -- the runtime
auto-RTL failsafe itself is covered by services/api/tests/test_api.py,
not duplicated here). This is the CI gate for "the vertical slice still
works" (see docs/PHASE_1_PLAN.md, section 8).

Assumes the three services' dependencies are already installed (either in
the active interpreter, or point PYTHON at a venv's python3).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.environ.get("PYTHON", sys.executable)
API_URL = "http://127.0.0.1:8000"
GATEWAY_URL = "http://127.0.0.1:8001"
GATEWAY2_URL = "http://127.0.0.1:8002"
VEHICLE_ID = "sim-1"
VEHICLE2_ID = "sim-2"
LOG_DIR = tempfile.mkdtemp(prefix="uas-smoke-")


def http_get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def http_post(url, payload, timeout=10):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def http_post_status(url, payload, timeout=10):
    """Like http_post, but returns (status_code, body) instead of raising
    on a non-2xx response -- used to assert on an expected error status,
    e.g. a geofence-violating mission upload's 422."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_for(predicate, timeout_s, label):
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            if predicate():
                return
        except (urllib.error.URLError, ConnectionError) as exc:
            last_err = exc
        time.sleep(0.5)
    raise TimeoutError(f"timed out waiting for: {label} (last error: {last_err})")


def main():
    env = dict(os.environ)
    env.setdefault("UAS_INTERNAL_TOKEN", "dev-secret")
    env.setdefault("VEHICLE_ID", VEHICLE_ID)

    procs = []

    def spawn(name, cwd, args, extra_env=None):
        e = dict(env)
        if extra_env:
            e.update(extra_env)
        log_path = os.path.join(LOG_DIR, f"{name}.log")
        log_file = open(log_path, "w")
        p = subprocess.Popen(args, cwd=cwd, env=e, stdout=log_file, stderr=subprocess.STDOUT)
        procs.append((name, p, log_path))
        return p

    try:
        print("Starting API...")
        spawn(
            "api",
            os.path.join(ROOT, "services", "api"),
            [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
            {
                "PYTHONPATH": ".",
                # Routes sim-2's commands to its own gateway (port 8002,
                # started later below) -- sim-1 uses GATEWAY_BASE_URL, the
                # default, unset here so it falls back correctly.
                "GATEWAY_URLS_JSON": json.dumps({VEHICLE2_ID: GATEWAY2_URL}),
            },
        )
        wait_for(lambda: http_get(f"{API_URL}/health").get("status") == "ok", 20, "API /health")
        print("API is up.")

        print("Starting telemetry gateway...")
        spawn(
            "gateway",
            os.path.join(ROOT, "services", "telemetry-gateway"),
            [PYTHON, "-m", "uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8001"],
            {"PYTHONPATH": ".", "API_BASE_URL": API_URL},
        )
        wait_for(lambda: http_get(f"{GATEWAY_URL}/health").get("status") == "ok", 20, "gateway /health")
        print("Gateway is up.")

        print("Starting simulator...")
        spawn(
            "sitl",
            os.path.join(ROOT, "simulation", "sitl"),
            [PYTHON, "sim.py", "--target-host", "127.0.0.1", "--target-port", "14550"],
        )

        print("Waiting for gateway <-> simulator MAVLink connection...")
        wait_for(
            lambda: http_get(f"{GATEWAY_URL}/health").get("mavlink_connected") is True,
            20,
            "gateway MAVLink connection (HEARTBEAT)",
        )
        print("Gateway sees HEARTBEAT from simulator.")

        print("Waiting for telemetry to reach the API...")
        wait_for(
            lambda: len(http_get(f"{API_URL}/vehicles/{VEHICLE_ID}/telemetry?limit=1")) >= 1,
            20,
            "first telemetry sample in API",
        )
        print("Telemetry flowing end-to-end.")

        print("Sending ARM command...")
        arm_result = http_post(f"{API_URL}/vehicles/{VEHICLE_ID}/commands", {"type": "ARM"})
        assert arm_result["status"] == "ACKED", f"ARM not acked: {arm_result}"
        print(f"  -> {arm_result}")

        wait_for(
            lambda: http_get(f"{API_URL}/vehicles/{VEHICLE_ID}").get("armed") is True,
            10,
            "vehicle armed state to propagate",
        )

        print("Sending TAKEOFF command (target 20m)...")
        takeoff_result = http_post(
            f"{API_URL}/vehicles/{VEHICLE_ID}/commands", {"type": "TAKEOFF", "altitude_m": 20}
        )
        assert takeoff_result["status"] == "ACKED", f"TAKEOFF not acked: {takeoff_result}"
        print(f"  -> {takeoff_result}")

        print("Waiting for altitude to climb...")

        def climbed():
            samples = http_get(f"{API_URL}/vehicles/{VEHICLE_ID}/telemetry?limit=1")
            if not samples:
                return False
            alt = samples[0].get("relative_alt_m")
            return alt is not None and alt > 2.0

        wait_for(climbed, 20, "relative_alt_m > 2.0m after takeoff")
        final = http_get(f"{API_URL}/vehicles/{VEHICLE_ID}/telemetry?limit=1")[0]
        print(f"Altitude climbing: relative_alt_m={final['relative_alt_m']:.1f}")

        print("Uploading a 2-waypoint mission close to home...")
        home_lat, home_lon = 37.4275, -122.1697
        waypoints = [
            {"lat": home_lat + 0.0003, "lon": home_lon, "alt_m": 25.0},
            {"lat": home_lat + 0.0003, "lon": home_lon + 0.0004, "alt_m": 25.0},
        ]
        # Explicit, generous timeout: the API's own internal timeout for a
        # mission upload is mission_upload_timeout_s (30s default, see
        # services/api/app/config.py) because the handshake is multiple
        # MAVLink round trips, not a single ack -- http_post()'s default
        # 10s client-side timeout is too tight and can fire first
        # (observed on Windows, where per-packet UDP latency runs higher
        # than in this project's Linux CI).
        mission = http_post(
            f"{API_URL}/vehicles/{VEHICLE_ID}/missions", {"waypoints": waypoints}, timeout=40
        )
        assert mission["status"] == "UPLOADED", f"mission upload not accepted: {mission}"
        print(f"  -> mission {mission['mission_id']} UPLOADED")

        print("Starting mission...")
        started = http_post(f"{API_URL}/vehicles/{VEHICLE_ID}/missions/{mission['mission_id']}/start", {})
        assert started["status"] == "ACTIVE", f"mission not started: {started}"
        print(f"  -> mission ACTIVE")

        print("Waiting for the vehicle to fly the mission to its last waypoint...")
        wait_for(
            lambda: http_get(f"{API_URL}/vehicles/{VEHICLE_ID}").get("current_waypoint_seq")
            == len(waypoints) - 1,
            30,
            f"current_waypoint_seq == {len(waypoints) - 1} (last waypoint)",
        )
        print("  -> reached the last waypoint.")

        print("Sending RTL command...")
        rtl_result = http_post(f"{API_URL}/vehicles/{VEHICLE_ID}/commands", {"type": "RTL"})
        assert rtl_result["status"] == "ACKED", f"RTL not acked: {rtl_result}"
        print(f"  -> {rtl_result}")

        print("Waiting for the vehicle to fly home, land, and disarm...")
        wait_for(
            lambda: http_get(f"{API_URL}/vehicles/{VEHICLE_ID}").get("armed") is False,
            30,
            "vehicle to disarm after RTL",
        )
        print("  -> vehicle disarmed (RTL complete).")

        print("Setting a geofence around home and confirming a far waypoint is rejected...")
        fence_points = [
            {"lat": home_lat - 0.001, "lon": home_lon - 0.001},
            {"lat": home_lat - 0.001, "lon": home_lon + 0.001},
            {"lat": home_lat + 0.001, "lon": home_lon + 0.001},
            {"lat": home_lat + 0.001, "lon": home_lon - 0.001},
        ]
        http_post(f"{API_URL}/vehicles/{VEHICLE_ID}/geofence", {"points": fence_points})

        status, body = http_post_status(
            f"{API_URL}/vehicles/{VEHICLE_ID}/missions",
            {"waypoints": [{"lat": home_lat + 1.0, "lon": home_lon, "alt_m": 20.0}]},
        )
        assert status == 422, f"expected mission outside the fence to be rejected, got {status}: {body}"
        print(f"  -> rejected as expected: {body['detail']}")

        print("Confirming a mission fully inside the same fence still uploads normally...")
        inside_mission = http_post(
            f"{API_URL}/vehicles/{VEHICLE_ID}/missions",
            {"waypoints": [{"lat": home_lat + 0.0002, "lon": home_lon, "alt_m": 20.0}]},
            timeout=40,
        )
        assert inside_mission["status"] == "UPLOADED", f"in-fence mission should upload: {inside_mission}"
        print(f"  -> mission {inside_mission['mission_id']} UPLOADED")

        print("Bringing up a second vehicle (sim-2) to test fleet command routing...")
        spawn(
            "gateway-2",
            os.path.join(ROOT, "services", "telemetry-gateway"),
            [PYTHON, "-m", "uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8002"],
            {
                "PYTHONPATH": ".",
                "API_BASE_URL": API_URL,
                "VEHICLE_ID": VEHICLE2_ID,
                "GATEWAY_MAVLINK_PORT": "14551",
            },
        )
        wait_for(lambda: http_get(f"{GATEWAY2_URL}/health").get("status") == "ok", 20, "gateway-2 /health")

        spawn(
            "sitl-2",
            os.path.join(ROOT, "simulation", "sitl"),
            [
                PYTHON, "sim.py", "--target-host", "127.0.0.1", "--target-port", "14551",
                "--home-lat", "37.4290", "--home-lon", "-122.1680",
            ],
        )
        wait_for(
            lambda: http_get(f"{GATEWAY2_URL}/health").get("mavlink_connected") is True,
            20,
            "gateway-2 MAVLink connection (HEARTBEAT)",
        )
        wait_for(
            lambda: len(http_get(f"{API_URL}/vehicles/{VEHICLE2_ID}/telemetry?limit=1")) >= 1,
            20,
            "sim-2 telemetry reaching the API",
        )
        print("  -> sim-2 telemetry flowing.")

        print("Sending ARM to sim-2 -- must reach gateway-2, not gateway-1...")
        arm2_result = http_post(f"{API_URL}/vehicles/{VEHICLE2_ID}/commands", {"type": "ARM"})
        assert arm2_result["status"] == "ACKED", f"sim-2 ARM not acked: {arm2_result}"

        wait_for(
            lambda: http_get(f"{API_URL}/vehicles/{VEHICLE2_ID}").get("armed") is True,
            10,
            "sim-2 armed state to propagate",
        )
        sim1_state = http_get(f"{API_URL}/vehicles/{VEHICLE_ID}")
        assert sim1_state["armed"] is False, (
            f"sim-1 should be untouched by sim-2's ARM command, but got: {sim1_state} "
            "-- this would mean multi-vehicle command routing is broken and every "
            "command is going to one gateway regardless of target vehicle"
        )
        print("  -> sim-2 armed; sim-1 untouched. Fleet command routing confirmed.")

        print("\nSMOKE TEST PASSED\n")
        return 0
    except Exception:
        print(f"\nSMOKE TEST FAILED -- logs in {LOG_DIR}\n")
        for name, _p, log_path in procs:
            print(f"--- {name} ({log_path}) ---")
            try:
                with open(log_path) as f:
                    print(f.read())
            except OSError:
                pass
        raise
    finally:
        for _name, p, _log_path in procs:
            p.terminate()
        deadline = time.time() + 5
        for _name, p, _log_path in procs:
            remaining = max(0, deadline - time.time())
            try:
                p.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
