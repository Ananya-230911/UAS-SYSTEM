from types import SimpleNamespace

import pytest

from app.core.models import GeofencePoint, SearchArea
from app.mission.goal_parser import (
    ClaudeGoalParser,
    MalformedProviderOutput,
    NeedsClarification,
    ParsedGoal,
    ProviderUnavailable,
    RuleBasedGoalParser,
    default_provider,
    parse_goal,
)

AREA = SearchArea(points=[GeofencePoint(lat=0, lon=0), GeofencePoint(lat=0, lon=1), GeofencePoint(lat=1, lon=1)])
FLEET = ["sim-2", "sim-1", "sim-3"]


def test_rule_based_reads_count_and_altitude():
    result = parse_goal("search the area inside the geofence with 2 drones at 35 m", FLEET, AREA)
    assert result.request.vehicle_ids == ["sim-1", "sim-2"]
    assert result.request.altitude_m == 35.0
    assert result.provider == "rule-based"


def test_number_words_are_understood():
    result = parse_goal("survey with three drones", FLEET, AREA)
    assert len(result.request.vehicle_ids) == 3


def test_no_count_means_all_available_and_default_altitude():
    result = parse_goal("search the area", FLEET, AREA, default_altitude_m=20.0)
    assert result.request.vehicle_ids == ["sim-1", "sim-2", "sim-3"]
    assert result.request.altitude_m == 20.0


def test_unclear_request_asks_instead_of_guessing():
    with pytest.raises(NeedsClarification):
        parse_goal("hello drones", FLEET, AREA)


def test_more_drones_than_available_asks():
    with pytest.raises(NeedsClarification, match="only 3 are available"):
        parse_goal("search the area with 5 drones", FLEET, AREA)


def test_missing_search_area_asks():
    with pytest.raises(NeedsClarification, match="no search area"):
        parse_goal("search the area", FLEET, None)


@pytest.mark.parametrize("text", ["attack the building", "drop payload on the target", "strike that car"])
def test_out_of_scope_requests_are_refused_before_any_provider_runs(text):
    class ExplodingProvider:
        name = "should-not-run"

        def parse(self, text):
            raise AssertionError("provider must not be called for out-of-scope text")

    with pytest.raises(NeedsClarification, match="outside what this system does"):
        parse_goal(text, FLEET, AREA, provider=ExplodingProvider())


def test_provider_down_falls_back_to_rules_and_says_so():
    class DownProvider:
        name = "claude (test)"

        def parse(self, text):
            raise ProviderUnavailable("connection refused")

    result = parse_goal("search the area with 2 drones", FLEET, AREA, provider=DownProvider())
    assert len(result.request.vehicle_ids) == 2
    assert "fallback" in result.provider and "connection refused" in result.provider


def test_malformed_provider_output_asks_the_operator():
    class GarbageProvider:
        name = "claude (test)"

        def parse(self, text):
            raise MalformedProviderOutput("not a ParsedGoal")

    with pytest.raises(NeedsClarification, match="couldn't reliably interpret"):
        parse_goal("search the area", FLEET, AREA, provider=GarbageProvider())


def test_provider_saying_unclear_becomes_its_question():
    class UnsureProvider:
        name = "claude (test)"

        def parse(self, text):
            return ParsedGoal(intent="unclear", clarifying_question="Which area?")

    with pytest.raises(NeedsClarification, match="Which area"):
        parse_goal("do the thing", FLEET, AREA, provider=UnsureProvider())


def test_zero_drones_is_not_silently_accepted():
    with pytest.raises(NeedsClarification):
        parse_goal("search with 0 drones", FLEET, AREA)


# --- ClaudeGoalParser against a fake client (no network, no API key) ---


def _fake_client(response):
    return SimpleNamespace(messages=SimpleNamespace(parse=lambda **kwargs: response))


def test_claude_parser_returns_the_validated_structured_output():
    goal = ParsedGoal(intent="area_search", num_vehicles=2, altitude_m=30)
    parser = ClaudeGoalParser(client=_fake_client(SimpleNamespace(stop_reason="end_turn", parsed_output=goal)))
    assert parser.parse("search with 2 drones at 30 m") == goal


def test_claude_refusal_is_treated_as_provider_unavailable():
    parser = ClaudeGoalParser(client=_fake_client(SimpleNamespace(stop_reason="refusal", parsed_output=None)))
    with pytest.raises(ProviderUnavailable):
        parser.parse("search the area")


def test_claude_response_without_structured_output_is_malformed():
    parser = ClaudeGoalParser(client=_fake_client(SimpleNamespace(stop_reason="end_turn", parsed_output=None)))
    with pytest.raises(MalformedProviderOutput):
        parser.parse("search the area")


def test_default_provider_is_rules_without_an_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert isinstance(default_provider(), RuleBasedGoalParser)


def test_default_provider_is_claude_with_an_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    assert isinstance(default_provider(), ClaudeGoalParser)


@pytest.mark.parametrize("text, place", [
    ("3 drones survey sector 15 area", "sector 15"),
    ("check the area around Connaught Place with 4 drones at 40 m", "Connaught Place"),
    ("3 drones survey IIT Delhi at 40 m", "IIT Delhi"),
    ("survey this area with 3 drones", None),
    ("search for people in this area", None),
    ("monitor the area with 1 drone", None),
])
def test_rules_take_the_named_place_as_written(text, place):
    from app.mission.goal_parser import RuleBasedGoalParser

    assert RuleBasedGoalParser().parse(text).place_name == place
