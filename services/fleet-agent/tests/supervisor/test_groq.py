"""app/llm/groq.py against a fake Groq server (httpx.MockTransport): no
network, no key needed. The real API was not reachable from the test
environment -- see docs/adr/0019."""
import json

import httpx
import pytest

from app.cbba.demo import sample_scenario
from app.llm.groq import DEFAULT_GROQ_MODEL, GROQ_URL, GroqJsonClient
from app.mission.goal_parser import (
    GroqGoalParser,
    MalformedProviderOutput,
    ParsedGoal,
    ProviderUnavailable,
    default_provider,
    parse_goal,
)
from app.core.models import GeofencePoint, SearchArea
from app.supervisor.instructions import GroqInstructionParser, interpret_instruction


def fake_groq(content=None, status=200, raise_exc=None, seen=None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        if raise_exc:
            raise raise_exc
        body = {"choices": [{"message": {"content": content}}]} if status == 200 else {"error": "x"}
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def client(**kw):
    return GroqJsonClient(api_key="gsk_test_not_real", http=fake_groq(**kw))


def test_sends_json_mode_request_with_bearer_key_and_model():
    seen = []
    c = GroqJsonClient(api_key="gsk_test_not_real", model="openai/gpt-oss-20b", http=fake_groq(content='{"intent":"unclear"}', seen=seen))
    c.complete_json("sys", "hello", ParsedGoal)
    req = seen[0]
    assert str(req.url) == GROQ_URL
    assert req.headers["Authorization"] == "Bearer gsk_test_not_real"
    payload = json.loads(req.content)
    assert payload["model"] == "openai/gpt-oss-20b"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0
    assert "JSON schema" in payload["messages"][0]["content"]


def test_valid_answer_is_validated_into_the_model():
    out = client(content='{"intent":"area_search","num_vehicles":2,"altitude_m":30}').complete_json("s", "u", ParsedGoal)
    assert out == ParsedGoal(intent="area_search", num_vehicles=2, altitude_m=30)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_http_errors_mean_unavailable(status):
    with pytest.raises(ProviderUnavailable, match=str(status)):
        client(status=status).complete_json("s", "u", ParsedGoal)


def test_network_failure_means_unavailable():
    with pytest.raises(ProviderUnavailable):
        client(raise_exc=httpx.ConnectError("no route")).complete_json("s", "u", ParsedGoal)


@pytest.mark.parametrize("content", ["not json", '{"intent":"fly_to_moon"}', '{"num_vehicles": -3, "intent": "area_search"}', None])
def test_bad_answers_are_malformed_not_guessed(content):
    with pytest.raises(MalformedProviderOutput):
        client(content=content).complete_json("s", "u", ParsedGoal)


def test_no_key_means_unavailable_without_any_request(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    seen = []
    with pytest.raises(ProviderUnavailable, match="not set"):
        GroqJsonClient(http=fake_groq(content="{}", seen=seen)).complete_json("s", "u", ParsedGoal)
    assert seen == []


def test_model_comes_from_env_with_a_safe_default(monkeypatch):
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    assert GroqJsonClient(api_key="k").model == DEFAULT_GROQ_MODEL
    monkeypatch.setenv("GROQ_MODEL", "qwen/some-model")
    assert GroqJsonClient(api_key="k").model == "qwen/some-model"


def test_groq_is_the_preferred_goal_provider_when_its_key_is_set(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "also-set")
    assert isinstance(default_provider(), GroqGoalParser)


AREA = SearchArea(points=[GeofencePoint(lat=0, lon=0), GeofencePoint(lat=0, lon=1), GeofencePoint(lat=1, lon=1)])


def test_goal_parsing_with_groq_and_fallback_to_rules():
    ok = GroqGoalParser(client=client(content='{"intent":"area_search","num_vehicles":2,"altitude_m":40}'))
    r = parse_goal("please check the area with a couple of drones, 40 metres up", ["a", "b", "c"], AREA, provider=ok)
    assert r.request.vehicle_ids == ["a", "b"] and r.request.altitude_m == 40 and r.provider.startswith("groq")
    down = GroqGoalParser(client=client(status=429))
    r = parse_goal("search the area with 3 drones", ["a", "b", "c"], AREA, provider=down)
    assert r.provider.startswith("rule-based (fallback: groq") and "429" in r.provider


def test_instruction_via_groq_then_rules_fallback():
    sc = sample_scenario()
    good = GroqInstructionParser(client=client(content=json.dumps(
        {"intents": [{"kind": "set_priority", "point_id": "P03-tower", "priority": "high"}, {"kind": "exclude_drone", "drone_id": "sim-2"}]}
    )))
    r = interpret_instruction("the tower matters most and sim-2 has a bad motor", sc, good)
    assert r.provider.startswith("groq") and [i.kind for i in r.batch.intents] == ["set_priority", "exclude_drone"]
    down = GroqInstructionParser(client=client(raise_exc=httpx.ConnectTimeout("t")))
    r = interpret_instruction("P03 high priority", sc, down)
    assert r.provider.startswith("rule-based (fallback") and r.batch.intents[0].point_id == "P03-tower"


def test_groq_cannot_smuggle_a_bid_or_route():
    """Anything outside the allowed intent kinds fails schema validation,
    so it never reaches the gate, CBBA or the map."""
    from app.mission.goal_parser import NeedsClarification

    sneaky = GroqInstructionParser(client=client(content=json.dumps(
        {"intents": [{"kind": "set_route", "drone_id": "sim-1", "waypoints": [[0, 0]]}]}
    )))
    with pytest.raises(NeedsClarification):
        interpret_instruction("do something clever", sample_scenario(), sneaky)


def test_out_of_scope_is_refused_before_groq_is_called():
    from app.mission.goal_parser import NeedsClarification

    seen = []
    p = GroqInstructionParser(client=GroqJsonClient(api_key="k", http=fake_groq(content="{}", seen=seen)))
    for text in ["attack the tower", "target sim-2's position", "drop the payload on P01", "destroy P03"]:
        with pytest.raises(NeedsClarification, match="outside what this system does"):
            interpret_instruction(text, sample_scenario(), p)
    assert seen == []
