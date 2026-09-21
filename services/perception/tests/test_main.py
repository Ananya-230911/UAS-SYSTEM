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
