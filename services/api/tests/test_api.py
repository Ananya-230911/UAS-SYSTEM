import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json = json_data
        self.text = str(json_data)

    def json(self):
        return self._json


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient so command/mission tests don't need a
    real telemetry-gateway process running. Branches on the URL path since
    /command and /mission have different success-body shapes."""

    last_request = None
    mission_response = FakeResponse(200, {"accepted": True, "count": 1})
    command_response = FakeResponse(200, {"acked": True, "mav_result": 0})

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        FakeAsyncClient.last_request = (url, json)
        if url.endswith("/mission"):
            return FakeAsyncClient.mission_response
        return FakeAsyncClient.command_response


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_cors_allows_cross_origin_requests(client):
    # Regression test: apps/gcs-web is served from a different origin than
    # the API by design (docs/adr/0005-frontend-stack.md), so every
    # fetch() call it makes is cross-origin. Without CORS headers, the
    # browser silently blocks the response and fetch() throws a generic
    # "TypeError: Failed to fetch" -- indistinguishable, from the UI's
    # perspective, from the server being unreachable, and with nothing
    # useful printed server-side either. This was observed live: telemetry
    # kept flowing (WebSocket connections aren't subject to CORS) while
    # every Arm/Takeoff/Upload command failed identically.
    preflight = client.options(
        "/vehicles/sim-1/commands",
        headers={
            "Origin": "http://localhost:8080",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"

    resp = client.post(
        "/vehicles/sim-1/commands",
        json={"type": "ARM"},
        headers={"Origin": "http://localhost:8080"},
    )
    assert resp.headers["access-control-allow-origin"] == "*"


def test_ingest_telemetry_requires_token(client):
    resp = client.post("/internal/telemetry", json={"vehicle_id": "sim-1"})
    assert resp.status_code == 401


def test_ingest_and_read_back_telemetry(client):
    payload = {
        "vehicle_id": "sim-1",
        "lat": 37.4275,
        "lon": -122.1697,
        "alt_m": 20.0,
        "armed": True,
        "flight_mode": "LOITER",
    }
    resp = client.post(
        "/internal/telemetry", json=payload, headers={"X-Internal-Token": "test-secret"}
    )
    assert resp.status_code == 200

    vehicle_resp = client.get("/vehicles/sim-1")
    assert vehicle_resp.status_code == 200
    assert vehicle_resp.json()["armed"] is True

    telem_resp = client.get("/vehicles/sim-1/telemetry")
    assert telem_resp.status_code == 200
    samples = telem_resp.json()
    assert len(samples) >= 1
    assert samples[-1]["lat"] == 37.4275


def test_unknown_vehicle_404(client):
    resp = client.get("/vehicles/does-not-exist")
    assert resp.status_code == 404


def test_create_command_calls_gateway_and_records_result(client, monkeypatch):
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)

    resp = client.post("/vehicles/sim-1/commands", json={"type": "ARM"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ACKED"
    assert body["mav_result"] == 0
    assert FakeAsyncClient.last_request[1] == {"type": "ARM", "altitude_m": None}

    fetched = client.get(f"/vehicles/sim-1/commands/{body['command_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["command_type"] == "ARM"


def test_create_mission_rejects_empty_waypoints(client):
    resp = client.post("/vehicles/sim-1/missions", json={"waypoints": []})
    assert resp.status_code == 422


def test_create_mission_uploads_via_gateway_and_starts(client, monkeypatch):
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    waypoints = [{"lat": 1.0, "lon": 2.0, "alt_m": 10.0}, {"lat": 3.0, "lon": 4.0, "alt_m": 20.0}]

    create_resp = client.post("/vehicles/sim-1/missions", json={"waypoints": waypoints})
    assert create_resp.status_code == 200
    mission = create_resp.json()
    assert mission["status"] == "UPLOADED"
    assert mission["waypoints"] == waypoints
    assert FakeAsyncClient.last_request[0].endswith("/mission")

    fetched = client.get(f"/vehicles/sim-1/missions/{mission['mission_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "UPLOADED"

    listed = client.get("/vehicles/sim-1/missions")
    assert listed.status_code == 200
    assert any(m["mission_id"] == mission["mission_id"] for m in listed.json())

    start_resp = client.post(f"/vehicles/sim-1/missions/{mission['mission_id']}/start")
    assert start_resp.status_code == 200
    started = start_resp.json()
    assert started["status"] == "ACTIVE"
    assert started["started_at"] is not None
    assert FakeAsyncClient.last_request[1] == {"type": "MISSION_START", "altitude_m": None}

    vehicle = client.get("/vehicles/sim-1").json()
    assert vehicle["active_mission_id"] == mission["mission_id"]


def test_mission_upload_failure_is_recorded(client, monkeypatch):
    class RejectingClient(FakeAsyncClient):
        async def post(self, url, json=None):
            FakeAsyncClient.last_request = (url, json)
            return FakeResponse(422, "vehicle rejected mission")

    monkeypatch.setattr("app.main.httpx.AsyncClient", RejectingClient)

    resp = client.post(
        "/vehicles/sim-1/missions",
        json={"waypoints": [{"lat": 1.0, "lon": 2.0, "alt_m": 10.0}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["error"] is not None


def test_start_mission_requires_uploaded_status(client, monkeypatch):
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    resp = client.post(
        "/vehicles/sim-1/missions", json={"waypoints": [{"lat": 1.0, "lon": 2.0, "alt_m": 10.0}]}
    )
    mission_id = resp.json()["mission_id"]

    # Start it once (succeeds, moves to ACTIVE)...
    first_start = client.post(f"/vehicles/sim-1/missions/{mission_id}/start")
    assert first_start.status_code == 200

    # ...starting an already-ACTIVE mission is rejected.
    second_start = client.post(f"/vehicles/sim-1/missions/{mission_id}/start")
    assert second_start.status_code == 400


def test_get_mission_404_for_unknown_id(client):
    resp = client.get("/vehicles/sim-1/missions/does-not-exist")
    assert resp.status_code == 404


def test_mission_upload_timeout_never_reports_an_empty_error(client, monkeypatch):
    # Regression test: some httpx exceptions (notably timeouts raised
    # internally by its transport layer) have str(exc) == "". Before
    # _error_message() existed, that produced mission.error = "", which
    # the UI's `error || detail || "unknown error"` fallback rendered as
    # an unhelpful "unknown error" with no clue what actually happened --
    # exactly what was observed uploading a 5-waypoint mission on Windows.
    class TimingOutClient(FakeAsyncClient):
        async def post(self, url, json=None):
            raise httpx.ReadTimeout("")  # empty message, like the real one

    monkeypatch.setattr("app.main.httpx.AsyncClient", TimingOutClient)

    resp = client.post(
        "/vehicles/sim-1/missions",
        json={"waypoints": [{"lat": 1.0, "lon": 2.0, "alt_m": 10.0}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["error"]  # must be truthy/non-empty
    assert "ReadTimeout" in body["error"]


def test_command_timeout_never_reports_an_empty_error(client, monkeypatch):
    class TimingOutClient(FakeAsyncClient):
        async def post(self, url, json=None):
            raise httpx.ConnectTimeout("")

    monkeypatch.setattr("app.main.httpx.AsyncClient", TimingOutClient)

    resp = client.post("/vehicles/sim-1/commands", json={"type": "ARM"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["error"]
    assert "ConnectTimeout" in body["error"]


def test_fleet_ws_receives_telemetry_for_every_vehicle(client):
    # Phase 3: /ws/fleet exists so the UI can see every vehicle on one
    # connection instead of opening one WebSocket per vehicle it discovers
    # (see docs/adr/0009-multi-vehicle-fleet.md). Post telemetry for two
    # different vehicles and confirm both arrive on the one fleet socket.
    with client.websocket_connect("/ws/fleet") as ws:
        client.post(
            "/internal/telemetry",
            json={"vehicle_id": "fleet-test-a", "armed": True, "flight_mode": "LOITER"},
            headers={"X-Internal-Token": "test-secret"},
        )
        client.post(
            "/internal/telemetry",
            json={"vehicle_id": "fleet-test-b", "armed": False, "flight_mode": "STANDBY"},
            headers={"X-Internal-Token": "test-secret"},
        )

        first = ws.receive_json()
        second = ws.receive_json()

    received_ids = {first["vehicle_id"], second["vehicle_id"]}
    assert received_ids == {"fleet-test-a", "fleet-test-b"}


def test_per_vehicle_ws_still_works_alongside_fleet_ws(client):
    # Regression guard: adding /ws/fleet must not change the existing
    # per-vehicle /ws/telemetry/{id} contract Phase 1/2 already rely on.
    with client.websocket_connect("/ws/telemetry/fleet-test-a") as ws:
        client.post(
            "/internal/telemetry",
            json={"vehicle_id": "fleet-test-a", "armed": True, "flight_mode": "TAKEOFF"},
            headers={"X-Internal-Token": "test-secret"},
        )
        client.post(
            "/internal/telemetry",
            json={"vehicle_id": "fleet-test-b", "armed": True, "flight_mode": "TAKEOFF"},
            headers={"X-Internal-Token": "test-secret"},
        )

        message = ws.receive_json()

    assert message["vehicle_id"] == "fleet-test-a"


def test_command_is_routed_to_the_correct_vehicles_gateway(client, monkeypatch):
    # Regression test for the Phase 3 multi-vehicle bug where every
    # command/mission call used one hardcoded gateway_base_url, so a
    # second vehicle's commands would silently go to the first vehicle's
    # gateway. GATEWAY_URLS_JSON maps vehicle_id -> its own gateway; a
    # vehicle_id missing from it falls back to gateway_base_url.
    from app.config import settings

    monkeypatch.setitem(settings.gateway_urls, "sim-2", "http://gateway-2:9999")
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)

    client.post("/vehicles/sim-2/commands", json={"type": "ARM"})
    assert FakeAsyncClient.last_request[0].startswith("http://gateway-2:9999")

    client.post("/vehicles/sim-1/commands", json={"type": "ARM"})
    assert FakeAsyncClient.last_request[0].startswith(settings.gateway_base_url)
