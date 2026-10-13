from fastapi.testclient import TestClient

from app.cbba.demo import sample_scenario
from app.cbba.service import allocate
from app.server import app
from app.store import InMemoryMissionStore

client = TestClient(app)


def test_store_versions_and_hash():
    s = InMemoryMissionStore()
    sc = sample_scenario()
    r = allocate(sc)
    v1 = s.add_version(sc, r)
    v2 = s.add_version(sc, r, mission_id=v1.mission_id, source="instruction", instruction="P03 high", changes=["Make P03-tower high priority"])
    assert (v1.version, v2.version) == (1, 2)
    assert v1.content_hash == v2.content_hash and len(v1.content_hash) == 64
    assert [x.version for x in s.get(v1.mission_id)] == [1, 2]
    assert s.list_missions()[0]["versions"] == 2
    assert s.get("nope") == []


def test_store_drops_oldest_mission_beyond_the_limit():
    s = InMemoryMissionStore(max_missions=2)
    sc, r = sample_scenario(), allocate(sample_scenario())
    ids = [s.add_version(sc, r).mission_id for _ in range(3)]
    assert s.get(ids[0]) == [] and s.get(ids[2])


def _scenario_json():
    return client.get("/cbba/sample").json()["scenario"]


def test_missions_plan_saves_versions():
    sc = _scenario_json()
    first = client.post("/missions/plan", json={"scenario": sc}).json()
    mid = first["mission"]["mission_id"]
    assert first["mission"]["version"] == 1 and first["result"]["dispatch_allowed"]
    second = client.post("/missions/plan", json={"scenario": sc, "mission_id": mid, "source": "instruction", "instruction": "replan", "changes": ["Re-plan with CBBA"]}).json()
    assert second["mission"]["version"] == 2 and second["mission"]["source"] == "instruction"
    hist = client.get(f"/missions/{mid}").json()
    assert [h["version"] for h in hist] == [1, 2] and hist[1]["changes"] == ["Re-plan with CBBA"]
    assert any(m["mission_id"] == mid for m in client.get("/missions").json())
    assert client.get("/missions/M-doesnotexist").status_code == 404


def test_instruct_returns_a_checked_proposal_without_planning(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    sc = _scenario_json()
    body = client.post("/cbba/instruct", json={"scenario": sc, "text": "P10 any drone, P99 high priority, sim-2 offline"}).json()
    assert body["provider"] == "rule-based"
    assert [a["change"] for a in body["accepted"]] == ["P10-mast can be done by any drone", "Take sim-2 out of the mission (offline)"]
    assert body["scenario"]["points"][9]["required_capability"] is None
    # nothing was planned or stored by asking
    assert "result" not in body


def test_instruct_questions_and_refusals():
    sc = _scenario_json()
    q = client.post("/cbba/instruct", json={"scenario": sc, "text": "hello there"}).json()
    assert q["question"] and q["scenario"] is None
    r = client.post("/cbba/instruct", json={"scenario": sc, "text": "attack the tower"}).json()
    assert "outside what this system does" in r["question"]


def test_ai_status_never_reveals_the_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret_value")
    body = client.get("/ai/status").json()
    assert body["groq_configured"] is True
    assert "gsk_secret_value" not in str(body)


def test_instruct_engines(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    sc = _scenario_json()
    rules = client.post("/cbba/instruct", json={"scenario": sc, "text": "P03 high priority", "engine": "rules"}).json()
    assert rules["provider"] == "rule-based"
    # crewai without crewai installed or without a key: falls back to rules, and says so
    crew = client.post("/cbba/instruct", json={"scenario": sc, "text": "P03 high priority", "engine": "crewai"}).json()
    assert crew["provider"].startswith("rule-based (fallback: crewai")
    assert crew["accepted"][0]["change"] == "Make P03-tower high priority"
    assert client.post("/cbba/instruct", json={"scenario": sc, "text": "x", "engine": "gpt"}).status_code == 422
