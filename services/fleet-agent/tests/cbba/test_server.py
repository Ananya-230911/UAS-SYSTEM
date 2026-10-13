"""The read-only CBBA HTTP API (app/server.py)."""
from fastapi.testclient import TestClient

from app.server import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok", "mode": "simulation only"}


def test_sample_returns_scenario_result_and_reference():
    body = client.get("/cbba/sample").json()
    assert body["result"]["label"] == "CBBA-sync-sim (in-process)"
    assert body["result"]["dispatch_allowed"] is True
    assert body["reference"]["same_allocation"] is True
    assert len(body["scenario"]["points"]) == 11
    assert len(body["scenario"]["geofence"]["points"]) == 4


def test_sample_split_comms_is_blocked():
    r = client.get("/cbba/sample", params={"comms": "split"}).json()["result"]
    assert r["dispatch_allowed"] is False
    assert {i["code"] for i in r["safety"]["issues"]} == {"DUPLICATE_ASSIGNMENT"}


def test_sample_offline_drone_is_left_out():
    r = client.get("/cbba/sample", params=[("offline", "sim-2")]).json()["result"]
    assert [x["item_id"] for x in r["excluded_drones"]] == ["sim-2"]
    assert r["dispatch_allowed"] is True


def test_bad_inputs_are_rejected():
    assert client.get("/cbba/sample", params={"comms": "mesh"}).status_code == 422
    assert client.get("/cbba/sample", params={"offline": "sim-9"}).status_code == 422


def test_allocate_any_scenario():
    scenario = client.get("/cbba/sample").json()["scenario"]
    r = client.post("/cbba/allocate", json=scenario)
    assert r.status_code == 200 and r.json()["converged"] is True
    assert client.post("/cbba/allocate", json={"drones": []}).status_code == 422


def test_no_command_endpoints_exist():
    """/missions/* only stores plans; nothing here can command a vehicle."""
    paths = {route.path for route in app.routes}
    words = ("command", "arm", "takeoff", "dispatch", "upload", "start", "rtl", "land", "fly")
    assert not any(w in p.lower() for p in paths for w in words), paths


def test_chain_with_offline_relay_does_not_crash():
    r = client.get("/cbba/sample", params=[("comms", "line"), ("offline", "sim-2")])
    assert r.status_code == 200
    assert r.json()["result"]["dispatch_allowed"] is False


def test_sample_includes_round_by_round_trace():
    body = client.get("/cbba/sample").json()
    trace = body["trace"]
    assert trace and trace[0]["round"] == 1
    assert {e["kind"] for e in trace[0]["events"]} >= {"bid"}
    # The last traced round's claims are the final plan.
    final = {p["drone_id"]: p["bundle"] for p in body["result"]["plans"]}
    assert trace[-1]["claims"] == final


def test_plan_accepts_an_operator_built_scenario():
    scenario = client.get("/cbba/sample").json()["scenario"]
    scenario["points"] = scenario["points"][:3]
    r = client.post("/cbba/plan", json=scenario)
    assert r.status_code == 200
    body = r.json()
    assert len(body["scenario"]["points"]) == 3 and body["result"]["converged"]
    assert client.post("/cbba/plan", json={"drones": [], "points": [], "geofence": {"points": []}}).status_code == 422
