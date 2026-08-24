#!/usr/bin/env python3
"""End-to-end Phase 1 smoke test.

Starts api + telemetry-gateway + sitl as subprocesses, waits for telemetry to
flow, arms the simulated vehicle, commands takeoff, and asserts altitude
increases. This is the CI gate for "the vertical slice still works" (see
docs/PHASE_1_PLAN.md, section 8).

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
VEHICLE_ID = "sim-1"
LOG_DIR = tempfile.mkdtemp(prefix="uas-smoke-")


def http_get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def http_post(url, payload, timeout=10):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


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
            {"PYTHONPATH": "."},
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
