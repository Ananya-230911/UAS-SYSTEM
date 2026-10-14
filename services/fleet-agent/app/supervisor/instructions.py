"""Operator instruction text -> IntentBatch (docs/CBBA_PLAN.md section 2B).

    "P3 and P5 are high priority, P2 needs thermal, D2 is offline"

Two providers, same contract as app/mission/goal_parser.py:
  - RuleBasedInstructionParser: keywords + the mission's own place/drone
    names. Deterministic, offline, no key. Always available.
  - GroqInstructionParser: Groq fills IntentBatch (JSON). Used when
    GROQ_API_KEY is set; unreachable/refusing -> rules take over, and the
    result says so.

Whatever a provider returns is only a *request*: IntentGate (gate.py)
validates it, CBBA re-plans, and the safety gate decides. Requests outside
this project's scope (weapons, attacks, targeting) are refused before any
provider runs -- same rule as the goal parser.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Protocol

from app.cbba.models import Scenario
from app.mission.goal_parser import (
    OUT_OF_SCOPE_PATTERN,
    MalformedProviderOutput,
    NeedsClarification,
    ProviderUnavailable,
)
from app.supervisor.intents import (
    ExcludeDrone,
    IncludeDrone,
    IntentBatch,
    RequestReplan,
    RequireSensor,
    SetDeadline,
    SetPriority,
    SkipPoint,
)

EXAMPLES = (
    "For example: 'P3 is high priority', 'P2 needs thermal', 'skip P5', "
    "'D2 is offline', 'D2 is back', 'P1 within 90 seconds', 'replan'."
)
OUT_OF_SCOPE_ANSWER = (
    "That's outside what this system does. It only plans simulated inspection missions -- "
    "no targeting, no attacks, no payloads. " + EXAMPLES
)
# Extra out-of-scope words for this panel, on top of the goal parser's list.
_TARGETING = re.compile(r"\b(target\w*|destroy\w*|hit|neutrali[sz]e\w*|enemy|enemies|hostile\w*)\b", re.IGNORECASE)


class InstructionParser(Protocol):
    name: str

    def parse(self, text: str, scenario: Scenario) -> IntentBatch: ...


# --- rules -------------------------------------------------------------------

_SEGMENT_SPLIT = re.compile(r"[.;\n]+|,|\band\b|\bthen\b|\balso\b", re.IGNORECASE)
_HIGH = re.compile(r"\b(high|urgent|important|critical|first)\b", re.IGNORECASE)
_LOW = re.compile(r"\b(low|unimportant|last|less important)\b", re.IGNORECASE)
_NORMAL = re.compile(r"\b(normal|medium|default)\s*(priority)?\b", re.IGNORECASE)
_SENSOR = re.compile(r"\b(thermal|lidar|camera)\b", re.IGNORECASE)
_ANY_SENSOR = re.compile(r"\bany\s+(drone|sensor|camera)\b", re.IGNORECASE)
_DEADLINE = re.compile(r"\b(?:within|in|under|before)\s+(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?)\b", re.IGNORECASE)
_NO_DEADLINE = re.compile(r"\bno\s+deadline\b|\bremove\s+(?:the\s+)?deadline\b", re.IGNORECASE)
_SKIP = re.compile(r"\b(skip|remove|ignore|cancel|drop|leave\s+out|don'?t\s+inspect)\b", re.IGNORECASE)
_DRONE_OUT = re.compile(r"\b(offline|down|broken|failed|lost|grounded|exclude|unavailable|not\s+available|out\s+of\s+service)\b", re.IGNORECASE)
_DRONE_IN = re.compile(r"\b(back|online|available|include|restored|fixed)\b", re.IGNORECASE)
_REPLAN = re.compile(r"\b(re-?plan|plan\s+again|recalculate|redo)\b", re.IGNORECASE)


def _find_ids(segment: str, ids: list[str]) -> list[str]:
    found = []
    for i in sorted(ids, key=len, reverse=True):  # longest first: "P10" before "P1"
        if re.search(rf"(?<![\w-]){re.escape(i)}(?![\w])", segment, re.IGNORECASE) and i not in found:
            found.append(i)
            segment = re.sub(rf"(?<![\w-]){re.escape(i)}(?![\w])", " ", segment, flags=re.IGNORECASE)
    return found


def _unpad(name: str) -> str:
    """"P04" -> "P4" (drop leading zeros of the number)."""
    return re.sub(r"(?<=[A-Za-z])0+(?=\d)", "", name)


def _id_aliases(point_ids: list[str]) -> dict[str, str]:
    """Let the operator write "P04" or "P4" for "P04-solar-panels"."""
    candidates: dict[str, list[str]] = {}
    for pid in point_ids:
        short = pid.split("-")[0]
        for name in {short, _unpad(short)}:
            if pid not in candidates.setdefault(name, []):
                candidates[name].append(pid)
    lowered = {p.lower() for p in point_ids}
    return {name: pids[0] for name, pids in candidates.items() if len(pids) == 1 and name.lower() not in lowered}


class RuleBasedInstructionParser:
    name = "rule-based"

    def parse(self, text: str, scenario: Scenario) -> IntentBatch:
        point_ids = [p.point_id for p in scenario.points]
        drone_ids = [d.drone_id for d in scenario.drones]
        aliases = _id_aliases(point_ids)
        all_point_names = point_ids + list(aliases)

        intents: list = []
        pending_points: list[str] = []
        pending_drones: list[str] = []
        for seg in _SEGMENT_SPLIT.split(text):
            if not seg.strip():
                continue
            pts = [aliases.get(x, x) for x in _find_ids(seg, all_point_names)]
            drs = _find_ids(seg, drone_ids)
            target_pts = pending_points + [p for p in pts if p not in pending_points]
            target_drs = pending_drones + [d for d in drs if d not in pending_drones]
            acted = False

            if _REPLAN.search(seg):
                intents.append(RequestReplan())
                acted = True
            if target_drs and _DRONE_OUT.search(seg):
                intents += [ExcludeDrone(drone_id=d) for d in target_drs]
                acted = True
            elif target_drs and _DRONE_IN.search(seg):
                intents += [IncludeDrone(drone_id=d) for d in target_drs]
                acted = True
            if target_pts:
                if _SKIP.search(seg):
                    intents += [SkipPoint(point_id=p) for p in target_pts]
                    acted = True
                else:
                    prio = "high" if _HIGH.search(seg) else "low" if _LOW.search(seg) else "normal" if _NORMAL.search(seg) else None
                    if prio:
                        intents += [SetPriority(point_id=p, priority=prio) for p in target_pts]
                        acted = True
                    if _ANY_SENSOR.search(seg):
                        intents += [RequireSensor(point_id=p, sensor="any") for p in target_pts]
                        acted = True
                    else:
                        sensor = _SENSOR.search(seg)
                        if sensor:
                            intents += [RequireSensor(point_id=p, sensor=sensor.group(1).lower()) for p in target_pts]
                            acted = True
                    if _NO_DEADLINE.search(seg):
                        intents += [SetDeadline(point_id=p, deadline_s=None) for p in target_pts]
                        acted = True
                    else:
                        dl = _DEADLINE.search(seg)
                        if dl:
                            secs = float(dl.group(1)) * (60 if dl.group(2).lower().startswith("m") else 1)
                            intents += [SetDeadline(point_id=p, deadline_s=secs) for p in target_pts]
                            acted = True

            if acted:
                pending_points, pending_drones = [], []
            else:
                # "P3 and P5 are high priority": P3's segment has no action
                # yet -- carry it to the next segment that has one.
                pending_points, pending_drones = target_pts, target_drs

        if not intents:
            return IntentBatch(clarifying_question="I didn't find a change I can make. " + EXAMPLES)
        return IntentBatch(intents=intents)


# --- Groq -------------------------------------------------------------------------

INSTRUCTION_SYSTEM_PROMPT = """\
You help an operator adjust a SIMULATED drone inspection mission. Turn their \
instruction into a list of allowed changes. You cannot plan routes, choose \
which drone goes where, place new points or move anything: the planner \
(CBBA) does that after your changes.

Allowed change kinds (use the exact ids listed below):
- set_priority {point_id, priority: low|normal|high}
- require_sensor {point_id, sensor: camera|thermal|lidar|any}
- set_deadline {point_id, deadline_s: seconds after take-off, or null to remove}
- skip_point {point_id}
- exclude_drone {drone_id}   (the drone is unavailable)
- include_drone {drone_id}   (the drone is available again)
- replan {}

If the instruction is unclear, or asks for anything else, return no changes \
and one short clarifying_question."""


class GroqInstructionParser:
    def __init__(self, client=None):
        if client is None:
            from app.llm.groq import GroqJsonClient

            client = GroqJsonClient()
        self._client = client
        self.name = f"groq ({client.model})"

    def parse(self, text: str, scenario: Scenario) -> IntentBatch:
        places = "; ".join(
            f"{p.point_id} (needs {p.required_capability or 'any drone'}, value {p.value:g}"
            + (f", deadline {p.deadline_s:g}s" if p.deadline_s else "")
            + ")"
            for p in scenario.points
        )
        drones = "; ".join(f"{d.drone_id} ({d.health.value}, sensors: {', '.join(d.capabilities) or 'none'})" for d in scenario.drones)
        system = f"{INSTRUCTION_SYSTEM_PROMPT}\n\nPlaces: {places}\nDrones: {drones}"
        return self._client.complete_json(system, text, IntentBatch)


def default_instruction_parser() -> InstructionParser:
    if os.environ.get("GROQ_API_KEY"):
        return GroqInstructionParser()
    return RuleBasedInstructionParser()


@dataclass
class InstructionResult:
    batch: IntentBatch
    provider: str  # which parser produced it, including any fallback


def interpret_instruction(text: str, scenario: Scenario, provider: InstructionParser | None = None) -> InstructionResult:
    """Instruction text -> requested changes (not yet checked or applied).
    Raises NeedsClarification with a question for the operator instead of
    guessing."""
    text = text.strip()
    if not text:
        raise NeedsClarification("Type what should change. " + EXAMPLES)
    if OUT_OF_SCOPE_PATTERN.search(text) or _TARGETING.search(text):
        raise NeedsClarification(OUT_OF_SCOPE_ANSWER)

    provider = provider or default_instruction_parser()
    name = provider.name
    try:
        batch = provider.parse(text, scenario)
    except ProviderUnavailable as exc:
        rules = RuleBasedInstructionParser()
        name = f"{rules.name} (fallback: {provider.name} unavailable -- {exc})"
        batch = rules.parse(text, scenario)
    except MalformedProviderOutput:
        raise NeedsClarification("I couldn't reliably interpret that. Could you rephrase it? " + EXAMPLES)

    if not batch.intents:
        raise NeedsClarification(batch.clarifying_question or ("I didn't find a change I can make. " + EXAMPLES))
    return InstructionResult(batch=batch, provider=name)
