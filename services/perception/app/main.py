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
import base64
import threading
import time

from fastapi import FastAPI, HTTPException
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


# One picture at a time: FastAPI runs these endpoints in parallel threads,
# but a webcam (and the one shared webcam_frame.jpg it is saved to) can
# only serve one capture at a time. Two drones reaching places in the same
# second used to make both captures fail.
_capture_lock = threading.Lock()


def _run_perception(vehicle_id: str, with_image: bool = False) -> dict:
    """Shared by both endpoints below: run detection + obstacle-finding
    once per call so a single /decision request doesn't silently run
    inference twice."""
    with _capture_lock:
        frame_path = camera.current_frame_path(vehicle_id)
        detections, image = detection.detect_objects_with_image(frame_path, with_image=with_image)
    tick = int(time.time())
    scan = lidar.current_scan(vehicle_id, tick)
    obstacles = obstacle.find_obstacles(scan)
    out = {
        "frame": frame_path.name,
        # Where the picture came from: the real webcam (PERCEPTION_USE_WEBCAM=1)
        # or one of the bundled sample photos.
        "camera": "webcam" if camera.USE_WEBCAM else "sample photo",
        "detections": detections,
        "lidar_obstacles": obstacles,
    }
    if with_image:
        out["image_jpeg_b64"] = base64.b64encode(image).decode("ascii") if image else None
    return out


@app.get("/vehicles/{vehicle_id}/perception")
def get_perception(vehicle_id: str, image: bool = False) -> dict:
    """Camera detections (real YOLOv8n inference on the current frame:
    the webcam if PERCEPTION_USE_WEBCAM=1, else a bundled sample photo)
    + LiDAR obstacle readings (simulated scan, real distance-threshold
    analysis). image=true also returns that frame as a JPEG with the
    detection boxes drawn on it. No recommendation here -- that's
    /decision below, which also needs telemetry context this endpoint
    doesn't require."""
    try:
        result = _run_perception(vehicle_id, with_image=image)
    except (RuntimeError, FileNotFoundError, ImportError) as exc:
        # Say what is wrong (no webcam, webcam busy, ultralytics not
        # installed ...) instead of a bare "Internal Server Error".
        raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}") from exc
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
