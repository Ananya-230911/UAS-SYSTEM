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
    """Stands in for httpx.AsyncClient so command tests don't need a real
    telemetry-gateway process running."""

    last_request = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        FakeAsyncClient.last_request = (url, json)
        return FakeResponse(200, {"acked": True, "mav_result": 0})


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


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
