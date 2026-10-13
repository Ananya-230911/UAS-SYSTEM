"""Optional CrewAI supervisor for operator instructions
(docs/CBBA_PLAN.md section 2B).

One CrewAI agent, three tools -- all thin wrappers around deterministic code:
  - get_mission_state: read-only summary of drones and places.
  - run_allocator: runs app.cbba.service.allocate() on the CURRENT inputs
    and returns who-does-what plus the safety verdict. Read-only: the
    agent sees the result and cannot change it.
  - request_change: records ONE requested change (an Intent from
    app/supervisor/intents.py). It does not apply it.

The crew's output is only the list of requested changes. The agent's
free-text "final answer" is ignored. Those changes then go through
IntentGate, CBBA re-plans, and the safety gate decides -- exactly like
the rule-based and Groq paths. CrewAI never computes, edits or overrides
bids, winners, bundles or routes (docs/CBBA_PLAN.md rule 1).

crewai is imported lazily; without it, CrewInstructionParser raises
ProviderUnavailable and the caller falls back to rules.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.cbba.models import Scenario
from app.cbba.service import allocate
from app.mission.goal_parser import ProviderUnavailable
from app.supervisor.instructions import INSTRUCTION_SYSTEM_PROMPT
from app.supervisor.intents import Intent, IntentBatch

_INTENT = TypeAdapter(Intent)
MAX_REQUESTS = 10


class RequestChangeArgs(BaseModel):
    kind: Literal["set_priority", "require_sensor", "set_deadline", "skip_point", "exclude_drone", "include_drone", "replan"]
    point_id: str | None = None
    drone_id: str | None = None
    priority: Literal["low", "normal", "high"] | None = None
    sensor: Literal["camera", "thermal", "lidar", "any"] | None = None
    deadline_s: float | None = Field(default=None, description="seconds after take-off; omit to remove a deadline")


class _Session:
    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.requested: list = []

    # --- tool bodies (plain functions, testable without crewai) ---------
    def mission_state(self) -> str:
        return json.dumps(
            {
                "drones": [
                    {"id": d.drone_id, "status": d.health.value, "battery_pct": round(d.battery_pct), "sensors": d.capabilities}
                    for d in self.scenario.drones
                ],
                "places": [
                    {"id": p.point_id, "needs": p.required_capability or "any drone", "value": p.value, "deadline_s": p.deadline_s}
                    for p in self.scenario.points
                ],
            }
        )

    def run_allocator(self) -> str:
        r = allocate(self.scenario)
        return json.dumps(
            {
                "label": r.label,
                "who_does_what": {p.drone_id: p.path for p in r.plans},
                "not_inspected": {x.item_id: x.reason for x in r.unassigned + r.excluded_points},
                "safety": "PASSED" if r.dispatch_allowed else "BLOCKED: " + "; ".join(i.code for i in r.safety.issues),
            }
        )

    def request_change(self, args: RequestChangeArgs) -> str:
        if len(self.requested) >= MAX_REQUESTS:
            return f"Refused: at most {MAX_REQUESTS} changes per instruction."
        raw = {k: v for k, v in args.model_dump().items() if v is not None or (k == "deadline_s" and args.kind == "set_deadline")}
        try:
            intent = _INTENT.validate_python(raw)
        except ValidationError as exc:
            return f"Refused: not a valid change ({exc.errors()[0]['msg']})."
        self.requested.append(intent)
        return f"Requested (not yet applied; the deterministic gate decides): {intent.describe()}"


def groq_llm(model: str | None = None, api_key: str | None = None):
    """CrewAI LLM pointed at Groq's OpenAI-compatible endpoint, without
    LiteLLM. CrewAI strips a leading 'openai/' from the model name when
    custom_openai=True, and Groq's own ids may start with 'openai/' (e.g.
    openai/gpt-oss-20b) -- so one extra 'openai/' is prefixed on purpose."""
    import os

    from crewai import LLM

    from app.llm.groq import DEFAULT_GROQ_MODEL

    groq_model = model or os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL
    key = api_key if api_key is not None else os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise ProviderUnavailable("GROQ_API_KEY is not set")
    return LLM(
        model=f"openai/{groq_model}",
        custom_openai=True,
        base_url="https://api.groq.com/openai/v1",
        api_key=key,
        temperature=0,
    )


def run_crew_supervisor(scenario: Scenario, instruction: str, llm) -> IntentBatch:
    try:
        from crewai import Agent, Crew, Process, Task
        from crewai.tools import tool
    except ImportError as exc:
        raise ProviderUnavailable("crewai is not installed (pip install -r requirements-crew.txt)") from exc

    session = _Session(scenario)

    @tool("get_mission_state")
    def get_mission_state() -> str:
        """Read-only: the drones (status, battery, sensors) and the places to inspect."""
        return session.mission_state()

    @tool("run_allocator")
    def run_allocator() -> str:
        """Read-only: run the deterministic CBBA planner on the current mission and return who inspects what and the safety verdict. You cannot change its result."""
        return session.run_allocator()

    @tool("request_change")
    def request_change(
        kind: str,
        point_id: str | None = None,
        drone_id: str | None = None,
        priority: str | None = None,
        sensor: str | None = None,
        deadline_s: float | None = None,
    ) -> str:
        """Request ONE change to the mission inputs. kind is one of: set_priority (point_id, priority low|normal|high),
        require_sensor (point_id, sensor camera|thermal|lidar|any), set_deadline (point_id, deadline_s or omit to remove),
        skip_point (point_id), exclude_drone (drone_id), include_drone (drone_id), replan. Nothing is applied until a
        deterministic safety gate checks it."""
        try:
            args = RequestChangeArgs(kind=kind, point_id=point_id, drone_id=drone_id, priority=priority, sensor=sensor, deadline_s=deadline_s)
        except ValidationError as exc:
            return f"Refused: not a valid change ({exc.errors()[0]['msg']})."
        return session.request_change(args)

    agent = Agent(
        role="Inspection mission supervisor",
        goal="Turn the operator's instruction into the smallest correct set of requested changes.",
        backstory=INSTRUCTION_SYSTEM_PROMPT,
        tools=[get_mission_state, run_allocator, request_change],
        llm=llm,
        allow_delegation=False,
        max_iter=8,
        verbose=False,
    )
    task = Task(
        description=(
            f"Operator instruction: {instruction!r}\n"
            "Look at the mission state, then call request_change once for each change the operator asked for. "
            "Do not invent changes they did not ask for. When done, reply with a one-line summary."
        ),
        expected_output="One-line summary of the requested changes.",
        agent=agent,
    )
    try:
        Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False).kickoff()
    except ProviderUnavailable:
        raise
    except Exception as exc:  # LLM/network/auth failures inside CrewAI
        raise ProviderUnavailable(f"CrewAI run failed: {type(exc).__name__}: {exc}") from exc
    return IntentBatch(intents=session.requested)


class CrewInstructionParser:
    """InstructionParser backed by the CrewAI supervisor (Groq LLM by default)."""

    def __init__(self, llm=None):
        self._llm = llm
        self.name = "crewai"

    def parse(self, text: str, scenario: Scenario) -> IntentBatch:
        llm = self._llm
        if llm is None:
            try:
                llm = groq_llm()
            except ImportError as exc:
                raise ProviderUnavailable("crewai is not installed (pip install -r requirements-crew.txt)") from exc
        self.name = f"crewai ({getattr(llm, 'model', 'llm')})"
        return run_crew_supervisor(scenario, text, llm)
