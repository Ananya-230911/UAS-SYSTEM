"""Inputs and outputs of the live-agent simulation (app/sim/world.py)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.cbba.models import CbbaResult, Scenario

SIM_LIVE_LABEL = "CBBA-async-sim (in-process, simulated lossy radio)"


class SimConfig(BaseModel):
    tick_s: float = Field(default=1.0, gt=0, le=10)  # one agent-loop step
    message_loss: float = Field(default=0.0, ge=0, le=0.9)  # chance any single message is lost
    message_delay_ticks: int = Field(default=0, ge=0, le=10)  # how late messages arrive
    heartbeat_timeout_s: float = Field(default=5.0, ge=2, le=120)  # silence before a teammate is declared lost
    max_time_s: float = Field(default=1800, gt=0, le=7200)
    max_planning_ticks: int = Field(default=200, ge=5, le=2000)
    seed: int = 0


class FailureEvent(BaseModel):
    """Something that happens to a drone at a given time (ground truth).
    The other drones are NOT told -- they have to notice."""

    at_s: float = Field(ge=0)
    kind: Literal["drone_lost", "radio_cut", "radio_restored", "battery_drop"]
    drone_id: str
    battery_pct: float | None = Field(default=None, ge=0, le=100)  # for battery_drop: the new level


class SimRequest(BaseModel):
    scenario: Scenario
    config: SimConfig = Field(default_factory=SimConfig)
    events: list[FailureEvent] = Field(default_factory=list, max_length=50)


Status = Literal["planning", "flying", "holding", "returning", "home", "lost", "offline", "lost-link"]


class DroneFrame(BaseModel):
    id: str
    lat: float
    lon: float
    battery: float
    status: Status
    epoch: int


class Frame(BaseModel):
    t: float
    drones: list[DroneFrame]
    inspected: list[str]
    links: list[tuple[str, str]]


class SimEvent(BaseModel):
    t: float
    kind: str  # e.g. "plan", "inspect", "detect", "replan", "lost-link", "inject", "home", "blocked"
    drone: str | None = None
    text: str


class EpochRecord(BaseModel):
    epoch: int
    started_t: float
    reason: str
    initiator: str | None
    participants: list[str]
    planning_ticks: int
    result: CbbaResult


class SimResult(BaseModel):
    label: str = SIM_LIVE_LABEL
    frames: list[Frame]
    events: list[SimEvent]
    epochs: list[EpochRecord]
    summary: dict
