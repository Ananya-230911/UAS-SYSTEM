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
    return detect_objects_with_image(frame_path, confidence_threshold, with_image=False)[0]


def detect_objects_with_image(
    frame_path, confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD, with_image: bool = True, max_width: int = 480
) -> tuple[list[dict], bytes | None]:
    """Same detections as detect_objects() from ONE inference, plus (if
    with_image) the frame as a JPEG with those boxes drawn on it, scaled
    down to max_width -- so the operator sees exactly what was detected."""
    model = _get_model()
    results = model(str(frame_path), verbose=False, conf=confidence_threshold)
    detections = []
    image = None
    for r in results:
        if with_image and image is None:
            import cv2  # opencv-python is already a dependency (ultralytics needs it too)

            plotted = r.plot()  # BGR array with boxes + labels
            h, w = plotted.shape[:2]
            if w > max_width:
                plotted = cv2.resize(plotted, (max_width, round(h * max_width / w)))
            ok, buf = cv2.imencode(".jpg", plotted, [cv2.IMWRITE_JPEG_QUALITY, 80])
            image = buf.tobytes() if ok else None
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
    return detections, image


# --- Aerial / satellite images (docs/adr/0022-geographic-imagery.md) ---------------
# The COCO model above was trained on photos taken from the ground; from
# straight above, cars and people look nothing like its training data.
# Ultralytics' YOLOv8-OBB model was trained on DOTA, a dataset of aerial
# and satellite images, so it is the right YOLOv8 for looking down. Its 15
# classes: plane, ship, storage tank, baseball diamond, tennis court,
# basketball court, ground track field, harbor, bridge, large vehicle,
# small vehicle, helicopter, roundabout, soccer ball field, swimming pool.
OBB_MODEL_PATH = Path(__file__).parent.parent / ".models" / "yolov8n-obb.pt"
AERIAL_CONFIDENCE_THRESHOLD = 0.25

_obb_model = None


def _get_obb_model():
    global _obb_model
    if _obb_model is None:
        from ultralytics import YOLO

        OBB_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _obb_model = YOLO(str(OBB_MODEL_PATH))  # weights auto-download on first use (~6 MB)
    return _obb_model


def detect_aerial(bgr, use_coco: bool = False, confidence_threshold: float = AERIAL_CONFIDENCE_THRESHOLD) -> list[dict]:
    """Run YOLOv8-OBB (aerial) -- and, for very sharp drone-level images,
    also the COCO model -- on exactly this BGR array. Returns detections
    with the model name, class, confidence and box corners in pixels."""
    out = []
    r = _get_obb_model()(bgr, verbose=False, conf=confidence_threshold, imgsz=1024)[0]
    if r.obb is not None:
        for poly, conf, cls in zip(r.obb.xyxyxyxy.tolist(), r.obb.conf.tolist(), r.obb.cls.tolist()):
            out.append({"model": "YOLOv8n-OBB (DOTA aerial)", "class_name": r.names[int(cls)],
                        "confidence": round(float(conf), 3), "polygon": [[round(x, 1), round(y, 1)] for x, y in poly]})
    if use_coco:
        r = _get_model()(bgr, verbose=False, conf=DEFAULT_CONFIDENCE_THRESHOLD)[0]
        for (x1, y1, x2, y2), conf, cls in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist()):
            out.append({"model": "YOLOv8n (COCO)", "class_name": r.names[int(cls)], "confidence": round(float(conf), 3),
                        "polygon": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]})
    return out
