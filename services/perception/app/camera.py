"""Simulated onboard camera (docs/adr/0015-perception-and-decision.md).

Cycles through a small set of bundled sample images per vehicle,
standing in for live camera frames -- there's no physical camera in
this project, the same simulation-first choice ADR-0001 already made
for the vehicle itself. What's real is the object detection that
actually runs on these frames (see detection.py), not the camera feed.
"""
import time
from pathlib import Path

FRAMES_DIR = Path(__file__).parent.parent / "sample_frames"


def available_frames() -> list[Path]:
    return sorted(FRAMES_DIR.glob("*.jpg"))


def current_frame_path(vehicle_id: str, now: float | None = None) -> Path:
    """Deterministic-but-changing: a different frame roughly every 5
    seconds, offset per vehicle_id so multiple vehicles don't always
    show the same frame at the same instant. `now` is injectable for
    tests; defaults to the real clock."""
    frames = available_frames()
    if not frames:
        raise FileNotFoundError(f"no sample frames found in {FRAMES_DIR}")
    if now is None:
        now = time.time()
    offset = sum(ord(c) for c in vehicle_id)
    index = (int(now // 5) + offset) % len(frames)
    return frames[index]
