"""HTTP API for the CBBA simulation (docs/CBBA_PLAN.md).

    cd services/fleet-agent
    PYTHONPATH=. uvicorn app.server:app --port 8020

It runs app.cbba.allocate() and returns the result so the GCS page can
draw it, keeps every planned version in memory (app/store.py), and turns
operator instructions into checked change requests (app/supervisor/). It
sends no commands and talks to no drone or gateway. The only outside call
it can make is to Groq, and only when GROQ_API_KEY is set -- to interpret
an instruction, never to plan or check safety. services/api is not
touched.
"""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.cbba.demo import COMMS, sample_scenario
from app.cbba.models import CbbaResult, Scenario
from app.cbba.service import allocate
from app.llm.groq import groq_configured
from app.mission.goal_parser import NeedsClarification
from app.store import InMemoryMissionStore, MissionStore, Source
from app.supervisor.gate import IntentGate
from app.supervisor.instructions import RuleBasedInstructionParser, interpret_instruction

app = FastAPI(title="UAS-SYSTEM Fleet-Agent (CBBA simulation)", version="0.1.0")

# Same reasoning as services/api and services/perception: apps/gcs-web is
# served from another origin, so its fetch() calls are cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# Version 1 storage: in memory, lost on restart. Swap for a SQLite-backed
# MissionStore later without changing the endpoints.
store: MissionStore = InMemoryMissionStore()
gate = IntentGate()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": "simulation only"}


@app.get("/ai/status")
def ai_status() -> dict:
    """Which instruction interpreter is active. Never reveals the key."""
    try:
        import crewai  # noqa: F401

        crew = True
    except ImportError:
        crew = False
    return {"groq_configured": groq_configured(), "crewai_installed": crew, "fallback": "rule-based (always available)"}


def _with_reference(scenario: Scenario) -> dict:
    trace: list = []
    result = allocate(scenario, trace=trace)
    reference = allocate(scenario, method="SGA")
    same = [(p.drone_id, p.path) for p in result.plans] == [(p.drone_id, p.path) for p in reference.plans]
    return {
        "scenario": scenario.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
        "reference": {"label": reference.label, "total_score": reference.total_score, "same_allocation": same},
        # Round-by-round record of how the drones decided (display only).
        "trace": [t.model_dump(mode="json") for t in trace],
    }


@app.get("/cbba/sample")
def cbba_sample(
    comms: Literal["full", "line", "split"] = "full",
    offline: list[str] = Query(default=[]),
) -> dict:
    """The built-in sample (3 drones, 11 inspection points near Stanford),
    with a chosen comms layout and optionally some drones offline."""
    known = {d.drone_id for d in sample_scenario().drones}
    unknown = sorted(set(offline) - known)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown drone id(s): {', '.join(unknown)}")
    assert comms in COMMS
    return _with_reference(sample_scenario(comms, offline))


@app.post("/cbba/plan")
def cbba_plan(scenario: Scenario) -> dict:
    """A scenario built by the operator in the GCS page (drones, inspection
    points, fence, radio links). Same response shape as /cbba/sample."""
    return _with_reference(scenario)


class PlanRequest(BaseModel):
    scenario: Scenario
    mission_id: str | None = None  # omit to start a new mission
    source: Source = "operator"
    instruction: str | None = Field(default=None, max_length=1000)
    changes: list[str] = Field(default_factory=list, max_length=50)


@app.post("/missions/plan")
def missions_plan(req: PlanRequest) -> dict:
    """Plan (CBBA + safety gate) and save the result as a new version of
    the mission. Same response as /cbba/plan, plus mission_id/version."""
    body = _with_reference(req.scenario)
    v = store.add_version(
        req.scenario,
        CbbaResult.model_validate(body["result"]),
        mission_id=req.mission_id,
        source=req.source,
        instruction=req.instruction,
        changes=req.changes,
    )
    body["mission"] = v.summary()
    return body


@app.get("/missions")
def missions_list() -> list[dict]:
    return store.list_missions()


@app.get("/missions/{mission_id}")
def missions_get(mission_id: str) -> list[dict]:
    versions = store.get(mission_id)
    if not versions:
        raise HTTPException(status_code=404, detail="no such mission")
    return [v.summary() for v in versions]


class InstructRequest(BaseModel):
    scenario: Scenario
    text: str = Field(min_length=1, max_length=1000)
    # auto: Groq if GROQ_API_KEY is set, else rules. rules: rules only.
    # crewai: the optional CrewAI supervisor (needs requirements-crew.txt
    # and GROQ_API_KEY). Any AI that is unavailable falls back to rules.
    engine: Literal["auto", "rules", "crewai"] = "auto"


@app.post("/cbba/instruct")
def cbba_instruct(req: InstructRequest) -> dict:
    """Operator instruction -> requested changes -> IntentGate. Returns the
    proposal; nothing is planned or saved until the operator applies it
    (POST /missions/plan with source="instruction")."""
    if req.engine == "rules":
        provider = RuleBasedInstructionParser()
    elif req.engine == "crewai":
        from app.crew.supervisor import CrewInstructionParser

        provider = CrewInstructionParser()
    else:
        provider = None
    try:
        interpreted = interpret_instruction(req.text, req.scenario, provider)
    except NeedsClarification as exc:
        return {"question": exc.question, "provider": None, "accepted": [], "rejected": [], "scenario": None}
    decision = gate.review(req.scenario, interpreted.batch.intents)
    return {
        "question": None,
        "provider": interpreted.provider,
        "accepted": [{"change": text, "intent": it.model_dump()} for it, text in decision.accepted],
        "rejected": [{"change": it.describe(), "reason": why, "intent": it.model_dump()} for it, why in decision.rejected],
        "scenario": decision.scenario.model_dump(mode="json") if decision.changed else None,
    }


@app.post("/cbba/allocate", response_model=CbbaResult)
def cbba_allocate(scenario: Scenario) -> CbbaResult:
    """Allocate any scenario. Returns the CBBA result, including the
    safety gate's verdict; dispatch_allowed is for later phases to read --
    nothing here dispatches."""
    return allocate(scenario)
