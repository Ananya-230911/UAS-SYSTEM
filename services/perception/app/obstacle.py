"""Obstacle detection over a simulated LiDAR scan
(docs/adr/0015-perception-and-decision.md).

Plain distance-threshold geometry over adjacent beams -- not machine
learning, a real (if simple) sensor-processing algorithm, matching
Modules 5/6 of the target architecture (acquisition/preprocessing),
which are about data handling, not AI -- the AI is specifically
detection.py (Module 7) and decision.py (Module 8).
"""

OBSTACLE_RANGE_THRESHOLD_M = 6.0
CLUSTER_GAP_DEG = 15.0  # adjacent close readings within this bearing gap merge into one obstacle


def find_obstacles(scan: list[dict]) -> list[dict]:
    """scan: chronological-by-bearing readings from lidar.current_scan().
    Groups consecutive close readings into obstacle clusters and returns
    the closest reading in each: [{"bearing_deg": float, "range_m": float}]."""
    close = [r for r in scan if r["range_m"] < OBSTACLE_RANGE_THRESHOLD_M]
    if not close:
        return []

    clusters = []
    current = [close[0]]
    for reading in close[1:]:
        if reading["bearing_deg"] - current[-1]["bearing_deg"] <= CLUSTER_GAP_DEG:
            current.append(reading)
        else:
            clusters.append(current)
            current = [reading]
    clusters.append(current)

    return [min(cluster, key=lambda r: r["range_m"]) for cluster in clusters]
