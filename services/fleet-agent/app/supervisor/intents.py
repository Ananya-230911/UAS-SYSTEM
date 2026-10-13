"""The only things an AI (Groq, CrewAI) -- or the rule-based parser -- may
ask for (docs/CBBA_PLAN.md section 2B).

Each intent is a request to change the mission's INPUTS. None of them can
carry a bid, a winner, a bundle, a route, a waypoint or a coordinate: an
AI cannot place a point or move a drone, and it never touches CBBA's
output. Every intent goes through IntentGate (gate.py) -- deterministic
code -- before anything changes, and the result is re-planned by CBBA and
re-checked by the safety gate as usual.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

PRIORITY_VALUE = {"low": 50.0, "normal": 100.0, "high": 150.0}


class SetPriority(BaseModel):
    kind: Literal["set_priority"] = "set_priority"
    point_id: str
    priority: Literal["low", "normal", "high"]

    def describe(self) -> str:
        return f"Make {self.point_id} {self.priority} priority"


class RequireSensor(BaseModel):
    kind: Literal["require_sensor"] = "require_sensor"
    point_id: str
    sensor: Literal["camera", "thermal", "lidar", "any"]

    def describe(self) -> str:
        return f"{self.point_id} can be done by any drone" if self.sensor == "any" else f"{self.point_id} needs a {self.sensor} sensor"


class SetDeadline(BaseModel):
    kind: Literal["set_deadline"] = "set_deadline"
    point_id: str
    deadline_s: float | None = Field(default=None, description="seconds after take-off; null removes the deadline")

    def describe(self) -> str:
        return f"Remove {self.point_id}'s deadline" if self.deadline_s is None else f"{self.point_id} must be reached within {self.deadline_s:g} s"


class SkipPoint(BaseModel):
    kind: Literal["skip_point"] = "skip_point"
    point_id: str

    def describe(self) -> str:
        return f"Leave {self.point_id} out of this mission"


class ExcludeDrone(BaseModel):
    kind: Literal["exclude_drone"] = "exclude_drone"
    drone_id: str

    def describe(self) -> str:
        return f"Take {self.drone_id} out of the mission (offline)"


class IncludeDrone(BaseModel):
    kind: Literal["include_drone"] = "include_drone"
    drone_id: str

    def describe(self) -> str:
        return f"Bring {self.drone_id} back into the mission"


class RequestReplan(BaseModel):
    kind: Literal["replan"] = "replan"

    def describe(self) -> str:
        return "Re-plan with CBBA"


Intent = Annotated[
    Union[SetPriority, RequireSensor, SetDeadline, SkipPoint, ExcludeDrone, IncludeDrone, RequestReplan],
    Field(discriminator="kind"),
]


class IntentBatch(BaseModel):
    """What a parser returns: a short list of intents, or a question for
    the operator when the instruction isn't clear."""

    intents: list[Intent] = Field(default_factory=list, max_length=20)
    clarifying_question: str | None = None
