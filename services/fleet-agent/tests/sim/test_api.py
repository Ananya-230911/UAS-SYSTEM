from fastapi.testclient import TestClient

import app.demo as demo
from app.core.models import Geofence, GeofencePoint, Waypoint
from app.planning.allocation import VehicleState
from app.server import app

client = TestClient(app)


def _scenario():
    return client.get("/cbba/sample").json()["scenario"]


def test_sim_run_returns_frames_events_and_summary():
    body = client.post("/sim/run", json={"scenario": _scenario(), "events": [{"at_s": 40, "kind": "drone_lost", "drone_id": "sim-2"}]}).json()
    assert body["label"].startswith("CBBA-async-sim")
    assert body["frames"][0]["t"] == 0 and len(body["frames"]) > 100
    assert body["summary"]["inspected_count"] == 9 and body["summary"]["planning_rounds"] == 2
    assert any(e["kind"] == "detect" for e in body["events"])


def test_sim_run_validates_inputs():
    sc = _scenario()
    assert client.post("/sim/run", json={"scenario": sc, "config": {"message_loss": 2}}).status_code == 422
    assert client.post("/sim/run", json={"scenario": sc, "events": [{"at_s": 1, "kind": "explode", "drone_id": "sim-1"}]}).status_code == 422


def test_fleet_live_reads_positions(monkeypatch):
    fence = Geofence(points=[GeofencePoint(lat=0, lon=0), GeofencePoint(lat=0, lon=1), GeofencePoint(lat=1, lon=1)])
    monkeypatch.setattr(demo, "load_live_fleet", lambda api, key: ({"sim-1": VehicleState("sim-1", Waypoint(lat=0.5, lon=0.6, alt_m=0), 88.04)}, fence, ["skipped sim-2: stale"]))
    body = client.get("/fleet/live").json()
    assert body["drones"] == [{"id": "sim-1", "lat": 0.5, "lon": 0.6, "battery": 88.0}]
    assert len(body["fence"]) == 3 and body["notes"] == ["skipped sim-2: stale"]


def test_fleet_live_reports_when_api_is_down(monkeypatch):
    monkeypatch.setenv("FLEET_API_URL", "http://127.0.0.1:9")  # nothing listens there
    r = client.get("/fleet/live")
    assert r.status_code == 503 and "could not read the live fleet" in r.json()["detail"]
