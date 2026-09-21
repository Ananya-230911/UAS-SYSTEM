"""Real object detection (docs/adr/0015-perception-and-decision.md).

Runs an actual pretrained YOLOv8n model (Ultralytics) on the current
simulated camera frame. The camera feed is simulated (camera.py); this
inference is not -- it's the same model class real drones use for
onboard detection, genuinely run, not faked or hardcoded, just against
a bundled still image instead of a live sensor.

Weights auto-download on first use (~6MB, needs network -- same
one-time-download-then-cached pattern as apps/gcs-web's OpenStreetMap
tiles) to .models/, which is gitignored: model weights are a build
artifact, not source to commit.
"""
from pathlib import Path

MODEL_PATH = Path(__file__).parent.parent / ".models" / "yolov8n.pt"
DEFAULT_CONFIDENCE_THRESHOLD = 0.4

_model = None


def _get_model():
    global _model
    if _model is None:
        from ultralytics import YOLO  # imported lazily -- see main.py's note on why

        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _model = YOLO(str(MODEL_PATH))
    return _model


def detect_objects(frame_path, confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> list[dict]:
    """Returns [{"class_name": str, "confidence": float, "bbox": {...}}, ...]
    for every detection at or above confidence_threshold."""
    model = _get_model()
    results = model(str(frame_path), verbose=False)
    detections = []
    for r in results:
        for box in r.boxes:
            conf = float(box.conf[0])
            if conf < confidence_threshold:
                continue
            cls_name = model.names[int(box.cls[0])]
            x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
            detections.append(
                {
                    "class_name": cls_name,
                    "confidence": round(conf, 3),
                    "bbox": {"x1": round(x1, 1), "y1": round(y1, 1), "x2": round(x2, 1), "y2": round(y2, 1)},
                }
            )
    return detections
