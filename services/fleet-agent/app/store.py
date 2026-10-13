"""Where planned missions are kept (docs/CBBA_PLAN.md decision 4).

MissionStore is the interface; InMemoryMissionStore is version 1 (lost
when the server restarts). A SqliteMissionStore can implement the same
three methods later without any caller changing.

Each saved plan is a new *version* of a mission: the first plan, every
re-plan after an operator instruction, and every re-plan after a drone
failure. A version records the exact inputs (scenario), CBBA's result
(including the safety verdict), a content hash of both, where it came
from, and the instruction text and accepted changes if any.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.cbba.models import CbbaResult, Scenario

Source = Literal["operator", "instruction", "failure-replan", "sample"]


class MissionVersion(BaseModel):
    mission_id: str
    version: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: Source = "operator"
    instruction: str | None = None
    changes: list[str] = Field(default_factory=list)
    scenario: Scenario
    result: CbbaResult
    content_hash: str = ""

    def summary(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "source": self.source,
            "instruction": self.instruction,
            "changes": self.changes,
            "dispatch_allowed": self.result.dispatch_allowed,
            "drones": len(self.scenario.drones),
            "places": len(self.scenario.points),
            "content_hash": self.content_hash,
        }


def content_hash(scenario: Scenario, result: CbbaResult) -> str:
    blob = json.dumps(
        {"scenario": scenario.model_dump(mode="json"), "result": result.model_dump(mode="json")},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


class MissionStore(Protocol):
    def add_version(
        self, scenario: Scenario, result: CbbaResult, mission_id: str | None = None, source: Source = "operator",
        instruction: str | None = None, changes: list[str] | None = None,
    ) -> MissionVersion: ...

    def get(self, mission_id: str) -> list[MissionVersion]: ...

    def list_missions(self) -> list[dict]: ...


class InMemoryMissionStore:
    """Thread-safe (uvicorn may serve requests concurrently). Keeps at most
    `max_missions`; the oldest mission is dropped first."""

    def __init__(self, max_missions: int = 200):
        self._lock = threading.Lock()
        self._missions: dict[str, list[MissionVersion]] = {}
        self.max_missions = max_missions

    def add_version(self, scenario, result, mission_id=None, source="operator", instruction=None, changes=None):
        with self._lock:
            if mission_id is None or mission_id not in self._missions:
                mission_id = mission_id or f"M-{uuid.uuid4().hex[:8]}"
                self._missions[mission_id] = []
                while len(self._missions) > self.max_missions:
                    del self._missions[next(iter(self._missions))]
            versions = self._missions[mission_id]
            v = MissionVersion(
                mission_id=mission_id,
                version=len(versions) + 1,
                source=source,
                instruction=instruction,
                changes=list(changes or []),
                scenario=scenario,
                result=result,
                content_hash=content_hash(scenario, result),
            )
            versions.append(v)
            return v

    def get(self, mission_id):
        with self._lock:
            return list(self._missions.get(mission_id, []))

    def list_missions(self):
        with self._lock:
            return [vs[-1].summary() | {"versions": len(vs)} for vs in self._missions.values()]
