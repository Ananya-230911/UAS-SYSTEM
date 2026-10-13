"""Read-only HTTP API for the CBBA simulation (docs/CBBA_PLAN.md).

    cd services/fleet-agent
    PYTHONPATH=. uvicorn app.server:app --port 8020

It runs app.cbba.allocate() and returns the result so the GCS page can
draw it. It sends no commands, talks to no drone, gateway or LLM, and
stores nothing (in-memory storage arrives with Phase D). services/api is
not touched.
"""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.cbba.demo import COMMS, sample_scenario
from app.cbba.models import CbbaResult, Scenario
from app.cbba.service import allocate

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


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": "simulation only"}


def _with_reference(scenario: Scenario) -> dict:
    result = allocate(scenario)
    reference = allocate(scenario, method="SGA")
    same = [(p.drone_id, p.path) for p in result.plans] == [(p.drone_id, p.path) for p in reference.plans]
    return {
        "scenario": scenario.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
        "reference": {"label": reference.label, "total_score": reference.total_score, "same_allocation": same},
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


@app.post("/cbba/allocate", response_model=CbbaResult)
def cbba_allocate(scenario: Scenario) -> CbbaResult:
    """Allocate any scenario. Returns the CBBA result, including the
    safety gate's verdict; dispatch_allowed is for later phases to read --
    nothing here dispatches."""
    return allocate(scenario)
