from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_perception_endpoint_returns_real_detections_and_a_lidar_scan():
    resp = client.get("/vehicles/sim-1/perception")
    assert resp.status_code == 200
    body = resp.json()
    assert body["vehicle_id"] == "sim-1"
    assert "frame" in body
    assert isinstance(body["detections"], list)
    assert isinstance(body["lidar_obstacles"], list)


def test_decision_endpoint_includes_a_recommendation():
    resp = client.get(
        "/vehicles/sim-1/decision",
        params={"armed": True, "battery_pct": 90, "gps_fix_type": 3},
    )
    assert resp.status_code == 200
    body = resp.json()
    rec = body["recommendation"]
    assert rec["action"] in {"CONTINUE", "HOLD", "DIVERT", "RTL"}
    assert rec["matched_guideline"]["id"].startswith("G")
    assert len(rec["guardrail_checks"]) == 3


def test_decision_endpoint_defaults_are_safe_when_query_params_omitted():
    resp = client.get("/vehicles/sim-1/decision")
    assert resp.status_code == 200
    assert resp.json()["recommendation"]["action"] in {"CONTINUE", "HOLD", "DIVERT", "RTL"}


def test_perception_can_return_the_picture_with_boxes(monkeypatch):
    """image=true: the same single inference also returns the frame as a
    JPEG with the boxes drawn, and says which camera took it."""
    import base64

    from app import detection

    calls = []

    def fake(frame_path, confidence_threshold=detection.DEFAULT_CONFIDENCE_THRESHOLD, with_image=True, max_width=480):
        calls.append(with_image)
        return [{"class_name": "person", "confidence": 0.9, "bbox": {"x1": 0, "y1": 0, "x2": 1, "y2": 1}}], b"JPEGBYTES"

    monkeypatch.setattr(detection, "detect_objects_with_image", fake)
    body = client.get("/vehicles/D1/perception", params={"image": "true"}).json()
    assert body["camera"] in {"webcam", "sample photo"}
    assert base64.b64decode(body["image_jpeg_b64"]) == b"JPEGBYTES"
    assert body["detections"][0]["class_name"] == "person"
    plain = client.get("/vehicles/D1/perception").json()
    assert "image_jpeg_b64" not in plain and calls == [True, False]   # one inference per request
