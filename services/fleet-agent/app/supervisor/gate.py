"""IntentGate: deterministic accept/reject for every requested change
(docs/CBBA_PLAN.md section 2B). It never trusts where an intent came from
-- rules, Groq, CrewAI or a person -- and applies only what passes:

  - the named point or drone must exist in this mission (ids are matched
    case-insensitively and rewritten to their real spelling);
  - values must be in range (deadline 1 s .. 2 h);
  - the mission must keep at least one drone and one place;
  - at most `max_per_batch` changes per instruction;
  - at most `max_replans_per_minute` instructions per minute (no replan
    storms -- an AI stuck in a loop gets refused, not obeyed).

The output is a NEW scenario (the input is never modified). Planning it
is still CBBA's job, and dispatching it is still the safety gate's call.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from app.cbba.models import DroneHealth, Scenario
from app.supervisor.intents import (
    PRIORITY_VALUE,
    ExcludeDrone,
    IncludeDrone,
    RequestReplan,
    RequireSensor,
    SetDeadline,
    SetPriority,
    SkipPoint,
)

MAX_DEADLINE_S = 7200.0


@dataclass
class GateDecision:
    scenario: Scenario
    accepted: list[tuple[object, str]] = field(default_factory=list)  # (intent, what changed)
    rejected: list[tuple[object, str]] = field(default_factory=list)  # (intent, why not)

    @property
    def changed(self) -> bool:
        return bool(self.accepted)


class IntentGate:
    def __init__(self, max_per_batch: int = 10, max_replans_per_minute: int = 6, clock=time.monotonic):
        self.max_per_batch = max_per_batch
        self.max_replans_per_minute = max_replans_per_minute
        self._clock = clock
        self._recent: deque[float] = deque()

    def review(self, scenario: Scenario, intents: list) -> GateDecision:
        sc = scenario.model_copy(deep=True)
        decision = GateDecision(scenario=sc)
        if not intents:
            return decision

        now = self._clock()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= self.max_replans_per_minute:
            for it in intents:
                decision.rejected.append((it, f"too many change requests this minute (limit {self.max_replans_per_minute}); wait and try again"))
            return decision
        self._recent.append(now)

        points = {p.point_id.lower(): p for p in sc.points}
        drones = {d.drone_id.lower(): d for d in sc.drones}

        for n, it in enumerate(intents):
            if n >= self.max_per_batch:
                decision.rejected.append((it, f"more than {self.max_per_batch} changes in one instruction"))
                continue
            reason = self._apply(sc, it, points, drones)
            if reason is None:
                decision.accepted.append((it, it.describe()))
            else:
                decision.rejected.append((it, reason))
        return decision

    def _apply(self, sc: Scenario, it, points: dict, drones: dict) -> str | None:
        """Apply one intent to `sc` in place; return a rejection reason or None."""
        if isinstance(it, RequestReplan):
            return None
        if isinstance(it, (SetPriority, RequireSensor, SetDeadline, SkipPoint)):
            p = points.get(it.point_id.lower())
            if p is None:
                return f"there is no place called '{it.point_id}' in this mission"
            it.point_id = p.point_id
            if isinstance(it, SetPriority):
                p.value = PRIORITY_VALUE[it.priority]
            elif isinstance(it, RequireSensor):
                p.required_capability = None if it.sensor == "any" else it.sensor
            elif isinstance(it, SetDeadline):
                if it.deadline_s is not None and not (1 <= it.deadline_s <= MAX_DEADLINE_S):
                    return f"a deadline must be between 1 s and {MAX_DEADLINE_S:.0f} s"
                p.deadline_s = it.deadline_s
            else:  # SkipPoint
                if len(sc.points) <= 1:
                    return "the mission must keep at least one place to inspect"
                sc.points = [q for q in sc.points if q is not p]
                del points[p.point_id.lower()]
            return None
        if isinstance(it, (ExcludeDrone, IncludeDrone)):
            d = drones.get(it.drone_id.lower())
            if d is None:
                return f"there is no drone called '{it.drone_id}' in this mission"
            it.drone_id = d.drone_id
            if isinstance(it, ExcludeDrone):
                still_up = [x for x in sc.drones if x.health != DroneHealth.OFFLINE and x is not d]
                if not still_up:
                    return "at least one drone must stay in the mission"
                d.health = DroneHealth.OFFLINE
            else:
                d.health = DroneHealth.OK
            return None
        return "that kind of change is not allowed"
