"""The optional CrewAI supervisor. Skipped unless crewai is installed
(pip install -r requirements-crew.txt) -- CI doesn't install it. Uses a
scripted stand-in LLM, so no network or key is needed: it exercises the
real CrewAI agent/tool loop end to end."""
import pytest

crewai = pytest.importorskip("crewai")

from app.cbba.demo import sample_scenario  # noqa: E402
from app.crew.supervisor import CrewInstructionParser, groq_llm, run_crew_supervisor  # noqa: E402
from app.mission.goal_parser import ProviderUnavailable  # noqa: E402
from app.supervisor.gate import IntentGate  # noqa: E402
from app.supervisor.instructions import interpret_instruction  # noqa: E402


class ScriptedLLM(crewai.BaseLLM):
    replies: list = []
    calls: int = 0

    def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None, from_agent=None, response_model=None, **kw):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply

    def supports_function_calling(self) -> bool:
        return False


def scripted(*replies):
    return ScriptedLLM(model="scripted-test-llm", replies=list(replies))


def act(tool, args="{}"):
    return f"Thought: next step\nAction: {tool}\nAction Input: {args}"


DONE = "Thought: I now know the final answer\nFinal Answer: done"


def test_crew_tools_produce_only_requested_changes():
    llm = scripted(
        act("get_mission_state"),
        act("run_allocator"),
        act("request_change", '{"kind": "set_priority", "point_id": "P03-tower", "priority": "high"}'),
        act("request_change", '{"kind": "exclude_drone", "drone_id": "sim-2"}'),
        DONE,
    )
    batch = run_crew_supervisor(sample_scenario(), "tower first, sim-2 has a bad motor", llm)
    assert [i.describe() for i in batch.intents] == ["Make P03-tower high priority", "Take sim-2 out of the mission (offline)"]
    assert llm.calls == 5


def test_crew_cannot_request_anything_outside_the_allowed_kinds():
    llm = scripted(
        act("request_change", '{"kind": "set_route", "drone_id": "sim-1"}'),
        act("request_change", '{"kind": "set_priority", "point_id": "P03-tower", "priority": "maximum"}'),
        DONE,
    )
    batch = run_crew_supervisor(sample_scenario(), "do whatever", llm)
    assert batch.intents == []


def test_crew_requests_still_go_through_the_gate():
    llm = scripted(act("request_change", '{"kind": "set_priority", "point_id": "P99", "priority": "high"}'), DONE)
    r = interpret_instruction("make P99 important", sample_scenario(), CrewInstructionParser(llm=llm))
    assert r.provider.startswith("crewai")
    decision = IntentGate().review(sample_scenario(), r.batch.intents)
    assert decision.accepted == [] and "no place called 'P99'" in decision.rejected[0][1]


def test_groq_llm_keeps_groq_model_ids_intact():
    for model in ["openai/gpt-oss-20b", "qwen/qwen3-32b"]:
        llm = groq_llm(model=model, api_key="gsk_test_not_real")
        assert llm.model == model
        assert llm.base_url == "https://api.groq.com/openai/v1"


def test_no_groq_key_means_unavailable(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailable):
        groq_llm()
