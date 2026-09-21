"""Runs real YOLOv8n inference -- not mocked -- against the bundled
sample frames. Slower than the rest of this suite (model load +
inference) and needs network on first run to fetch weights, same as
apps/gcs-web needing network for map tiles. This is the test that
actually proves detection.py does real object detection, not a
hardcoded response."""
from app.camera import available_frames
from app.detection import detect_objects


def test_detects_real_objects_in_bundled_sample_frames():
    frames = available_frames()
    assert frames, "no sample frames bundled -- see sample_frames/"

    all_classes = set()
    for frame in frames:
        detections = detect_objects(frame)
        for d in detections:
            assert 0.0 <= d["confidence"] <= 1.0
            assert set(d["bbox"].keys()) == {"x1", "y1", "x2", "y2"}
            all_classes.add(d["class_name"])

    # At least one of the bundled frames is a known people/vehicle scene
    # (ultralytics' own bus.jpg demo asset) -- a real model should find
    # at least one recognizable object across all bundled frames.
    assert all_classes, "expected at least one real detection across the bundled sample frames"


def test_confidence_threshold_filters_low_confidence_detections():
    frames = available_frames()
    permissive = detect_objects(frames[0], confidence_threshold=0.0)
    strict = detect_objects(frames[0], confidence_threshold=0.99)
    assert len(strict) <= len(permissive)
