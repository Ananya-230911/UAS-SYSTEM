"""Simulated LiDAR point cloud (docs/adr/0015-perception-and-decision.md).

Generates a synthetic set of range readings around the vehicle -- there
is no physical LiDAR in this project. obstacle.py's analysis over this
data is real, simple geometric processing (distance thresholding), just
running on synthetic input instead of a live sensor -- the same
simulate-the-input/keep-the-processing-real split as camera.py and
detection.py.
"""
import hashlib

NUM_BEAMS = 36  # one reading every 10 degrees -- a full horizontal sweep
MIN_CLEAR_RANGE_M = 8.0
MAX_RANGE_M = 30.0
OBSTACLE_CHANCE_PCT = 30  # how often a tick injects a close obstacle cluster
OBSTACLE_CLUSTER_WIDTH = 3
OBSTACLE_RANGE_MIN_M = 2.0
OBSTACLE_RANGE_SPAN_M = 3.0  # obstacle ranges land in [OBSTACLE_RANGE_MIN_M, +SPAN]


def _seed(vehicle_id: str, tick: int) -> int:
    digest = hashlib.sha256(f"{vehicle_id}:{tick}".encode()).hexdigest()
    return int(digest[:8], 16)


def current_scan(vehicle_id: str, tick: int) -> list[dict]:
    """Returns NUM_BEAMS readings, each {"bearing_deg": float, "range_m":
    float}, ordered by bearing. Deterministic given (vehicle_id, tick) --
    same tick always returns the same scan, which is both how a real
    sensor sampled at one instant behaves and what makes this testable."""
    state = _seed(vehicle_id, tick)
    readings = []
    for i in range(NUM_BEAMS):
        bearing = i * (360 / NUM_BEAMS)
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF  # simple deterministic LCG step
        frac = (state % 10000) / 10000
        range_m = MIN_CLEAR_RANGE_M + frac * (MAX_RANGE_M - MIN_CLEAR_RANGE_M)
        readings.append({"bearing_deg": round(bearing, 1), "range_m": round(range_m, 2)})

    state = (state * 1103515245 + 12345) & 0x7FFFFFFF
    if (state % 100) < OBSTACLE_CHANCE_PCT:
        cluster_start = state % NUM_BEAMS
        close_range = OBSTACLE_RANGE_MIN_M + (state % 300) / 100
        for offset in range(OBSTACLE_CLUSTER_WIDTH):
            idx = (cluster_start + offset) % NUM_BEAMS
            readings[idx]["range_m"] = round(close_range, 2)
    return readings
