"""AI Perception & Decision service (docs/adr/0015-perception-and-decision.md).

A separate process/service from services/api, deliberately -- its one
heavy dependency (ultralytics -> torch, several hundred MB) shouldn't be
forced onto everyone running the core GCS stack, the same reasoning
that keeps this out of services/api's own requirements.txt. It has no
database, no auth of its own (deployable behind the same reverse proxy
or API key scheme as the rest of the stack if that's ever needed -- see
the ADR's known limitations), and no persistence: every response is
computed fresh from the current simulated camera frame/LiDAR scan.
"""
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import camera, decision as decision_module, detection, lidar, obstacle

app = FastAPI(title="UAS-SYSTEM Perception & Decision Service", version="0.1.0")

# Same reasoning as services/api/app/main.py's CORS setup: apps/gcs-web
# is served from a different origin, so its fetch() calls to this
# service are cross-origin too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _run_perception(vehicle_id: str) -> dict:
    """Shared by both endpoints below: run detection + obstacle-finding
    once per call so a single /decision request doesn't silently run
    inference twice."""
    frame_path = camera.current_frame_path(vehicle_id)
    detections = detection.detect_objects(frame_path)
    tick = int(time.time())
    scan = lidar.current_scan(vehicle_id, tick)
    obstacles = obstacle.find_obstacles(scan)
    return {"frame": frame_path.name, "detections": detections, "lidar_obstacles": obstacles}


@app.get("/vehicles/{vehicle_id}/perception")
def get_perception(vehicle_id: str) -> dict:
    """Camera detections (real YOLOv8n inference on the current
    simulated frame) + LiDAR obstacle readings (simulated scan, real
    distance-threshold analysis). No recommendation here -- that's
    /decision below, which also needs telemetry context this endpoint
    doesn't require."""
    result = _run_perception(vehicle_id)
    return {"vehicle_id": vehicle_id, **result}


@app.get("/vehicles/{vehicle_id}/decision")
def get_decision(
    vehicle_id: str,
    armed: bool = False,
    battery_pct: float | None = None,
    gps_fix_type: int | None = None,
) -> dict:
    """Perception plus a RAG+guardrails recommendation. armed/battery_pct/
    gps_fix_type come from the caller (the UI already has this vehicle's
    live telemetry from services/api -- passed through as query params
    rather than this service maintaining its own copy of vehicle state)."""
    result = _run_perception(vehicle_id)
    rec = decision_module.recommend_action(
        result["detections"], result["lidar_obstacles"], armed, battery_pct, gps_fix_type
    )
    return {
        "vehicle_id": vehicle_id,
        **result,
        "recommendation": {
            "action": rec.action,
            "reasoning": rec.reasoning,
            "matched_guideline": rec.matched_guideline,
            "guardrail_checks": rec.guardrail_checks,
        },
    }
