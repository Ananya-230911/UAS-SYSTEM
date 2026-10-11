"""Onboard camera (docs/adr/0015-perception-and-decision.md).

Defaults to cycling through a small set of bundled sample images per
vehicle, standing in for live camera frames -- there's no physical
camera in this project's base case, the same simulation-first choice
ADR-0001 already made for the vehicle itself.

Set PERCEPTION_USE_WEBCAM=1 to capture real frames from the local
system webcam instead -- genuinely live sensor input (not simulated)
for a machine that actually has a camera attached. Off by default so
tests/CI (no camera present) stay deterministic and fast; this is an
explicit opt-in, read once at import time, not autodetected -- the
same "off unless you ask for it" pattern API_KEY already uses in
services/api/app/config.py.
"""
import os
import time
from pathlib import Path

FRAMES_DIR = Path(__file__).parent.parent / "sample_frames"
CACHE_DIR = Path(__file__).parent.parent / ".cache"
WEBCAM_FRAME_PATH = CACHE_DIR / "webcam_frame.jpg"

USE_WEBCAM = os.environ.get("PERCEPTION_USE_WEBCAM", "") == "1"

_webcam = None


def _get_webcam():
    """Lazily open the system webcam (device 0) once, reused across
    calls -- re-opening it on every request would be slow and can
    fail under contention. Only ever called when USE_WEBCAM is set."""
    global _webcam
    if _webcam is not None:
        return _webcam
    import cv2  # imported lazily: opencv-python is only needed on this path

    cam = cv2.VideoCapture(0)
    if not cam.isOpened():
        cam.release()
        raise RuntimeError(
            "PERCEPTION_USE_WEBCAM=1 but no webcam could be opened (device 0) -- "
            "check a camera is attached and not in use by another application"
        )
    _webcam = cam
    return _webcam


def available_frames() -> list[Path]:
    return sorted(FRAMES_DIR.glob("*.jpg"))


def current_frame_path(vehicle_id: str, now: float | None = None) -> Path:
    """A real, freshly-captured webcam frame when USE_WEBCAM is set;
    otherwise the simulated per-vehicle sample-image cycling: a
    different frame roughly every 5 seconds, offset per vehicle_id so
    multiple vehicles don't always show the same frame at the same
    instant. `now` is injectable for tests; defaults to the real clock
    (unused when USE_WEBCAM is set, since there's no "tick" to bucket --
    every call reads whatever the camera sees right now)."""
    if USE_WEBCAM:
        import cv2

        ok, frame = _get_webcam().read()
        if not ok:
            raise RuntimeError("failed to read a frame from the webcam")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(WEBCAM_FRAME_PATH), frame)
        return WEBCAM_FRAME_PATH

    frames = available_frames()
    if not frames:
        raise FileNotFoundError(f"no sample frames found in {FRAMES_DIR}")
    if now is None:
        now = time.time()
    offset = sum(ord(c) for c in vehicle_id)
    index = (int(now // 5) + offset) % len(frames)
    return frames[index]
