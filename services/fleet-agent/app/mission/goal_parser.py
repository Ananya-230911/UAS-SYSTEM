"""Operator text -> MissionRequest (docs/IMPLEMENTATION_PLAN.md Phase 3).

Two interchangeable providers behind one small interface:
  - RuleBasedGoalParser: keyword/regex rules. Deterministic, no network,
    no API key. Always available -- the default.
  - GroqGoalParser: asks Groq (free tier, preferred) to fill the same
    fixed schema. Used when GROQ_API_KEY is set (app/llm/groq.py).
  - ClaudeGoalParser: asks Claude to fill a fixed schema. Used only when
    ANTHROPIC_API_KEY is set (and no Groq key); the `anthropic` package is
    imported lazily so nothing else in this service depends on it.

Rules every provider's output goes through, regardless of which one ran:
  - The AI only interprets the request. It never sees vehicles, never
    chooses routes, and never issues commands -- planning and safety
    checks are deterministic code (app/planning, app/safety).
  - Its output must validate against ParsedGoal (Pydantic). Output that
    doesn't, or that says the request is unclear, becomes a question for
    the operator (NeedsClarification) -- never a guess.
  - If the provider is unreachable or refuses, the rule-based parser
    takes over, and the result says so.
  - Requests outside this project's scope (weapons, attacks) are refused
    before any provider is called -- see docs/IMPLEMENTATION_PLAN.md
    section 3, "Out of scope".
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from app.core.models import MissionRequest, SearchArea

CLAUDE_MODEL = "claude-opus-5-5"

OUT_OF_SCOPE_PATTERN = re.compile(
    r"\b(weapons?|attack\w*|strike|strikes|bomb\w*|missiles?|shoot\w*|kill\w*|engage\w*|"
    r"payload\s+release|drop\s+(?:the\s+)?payload)\b",
    re.IGNORECASE,
)


class NeedsClarification(Exception):
    """The request can't be planned as-is; `question` is what to ask the
    operator."""

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


class ProviderUnavailable(Exception):
    """The AI provider couldn't be reached or declined -- fall back to rules."""


class MalformedProviderOutput(Exception):
    """The AI provider answered, but not with a usable ParsedGoal."""


class ParsedGoal(BaseModel):
    """The only thing a provider is allowed to produce."""

    intent: Literal["area_search", "unclear"]
    num_vehicles: int | None = Field(default=None, ge=1)
    altitude_m: float | None = Field(default=None, gt=0)
    clarifying_question: str | None = None


class GoalParserProvider(Protocol):
    name: str

    def parse(self, text: str) -> ParsedGoal: ...


_INTENT_WORDS = re.compile(
    r"\b(search\w*|survey\w*|scan\w*|cover\w*|sweep\w*|recon\w*|reconnaissance|inspect\w*|"
    r"patrol\w*|explore\w*|map(?:ping)?|look\s+for|find)\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_VEHICLE_COUNT = re.compile(
    r"\b(\d+|" + "|".join(_NUMBER_WORDS) + r")\s+(?:drones?|vehicles?|uavs?|uas|quadcopters?)\b",
    re.IGNORECASE,
)
_ALTITUDE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:m|meters?|metres?)\b", re.IGNORECASE)

UNCLEAR_QUESTION = (
    "I can plan area search, survey or inspection missions. What should the drones do? "
    "For example: 'search the area inside the geofence with 3 drones at 30 m'."
)


class RuleBasedGoalParser:
    name = "rule-based"

    def parse(self, text: str) -> ParsedGoal:
        if not _INTENT_WORDS.search(text):
            return ParsedGoal(intent="unclear", clarifying_question=UNCLEAR_QUESTION)

        num_vehicles = None
        count = _VEHICLE_COUNT.search(text)
        if count:
            token = count.group(1).lower()
            num_vehicles = int(token) if token.isdigit() else _NUMBER_WORDS[token]

        altitude_m = None
        alt = _ALTITUDE.search(text)
        if alt:
            altitude_m = float(alt.group(1))

        # num_vehicles/altitude_m of 0 would fail ParsedGoal's own
        # validation -- treat that as "not a usable answer", same as a
        # malformed AI response.
        try:
            return ParsedGoal(intent="area_search", num_vehicles=num_vehicles, altitude_m=altitude_m)
        except ValidationError as exc:
            raise MalformedProviderOutput(str(exc)) from exc


GOAL_SYSTEM_PROMPT = """\
You turn a drone operator's request into a structured request for a SIMULATED \
search-and-survey mission planner. The planner supports only area search, \
survey, inspection and coverage missions.

Set intent to "area_search" when the request clearly asks for one of those. \
Otherwise set intent to "unclear" and write one short clarifying_question for \
the operator.

Fill num_vehicles and altitude_m only if the operator stated them (altitude in \
metres). If they did not, leave them null -- the planner has its own defaults, \
and a guessed value is worse than none."""


class ClaudeGoalParser:
    name = f"claude ({CLAUDE_MODEL})"

    def __init__(self, client=None):
        self._client = client

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ProviderUnavailable("the 'anthropic' package is not installed") from exc
            self._client = anthropic.Anthropic()
        return self._client

    def parse(self, text: str) -> ParsedGoal:
        client = self._get_client()
        try:
            response = client.messages.parse(
                model=CLAUDE_MODEL,
                max_tokens=16000,
                system=GOAL_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
                output_format=ParsedGoal,
            )
        except ValidationError as exc:
            raise MalformedProviderOutput(str(exc)) from exc
        except Exception as exc:
            # Imported lazily, so the anthropic exception classes can't
            # be named here without making the package mandatory. Narrow
            # to them when it is installed; anything else is a bug and
            # is re-raised, not swallowed.
            if _is_anthropic_api_error(exc):
                raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
            raise

        if response.stop_reason == "refusal":
            raise ProviderUnavailable("the model declined the request")
        if response.stop_reason == "max_tokens":
            raise MalformedProviderOutput("response was cut off before it finished")
        parsed = response.parsed_output
        if not isinstance(parsed, ParsedGoal):
            raise MalformedProviderOutput("no structured output in the response")
        return parsed


class GroqGoalParser:
    """Same contract as ClaudeGoalParser, via app/llm/groq.py."""

    def __init__(self, client=None):
        if client is None:
            from app.llm.groq import GroqJsonClient

            client = GroqJsonClient()
        self._client = client
        self.name = f"groq ({client.model})"

    def parse(self, text: str) -> ParsedGoal:
        return self._client.complete_json(GOAL_SYSTEM_PROMPT, text, ParsedGoal)


def _is_anthropic_api_error(exc: Exception) -> bool:
    try:
        import anthropic
    except ImportError:
        return False
    return isinstance(exc, (anthropic.APIConnectionError, anthropic.APIStatusError))


def default_provider() -> GoalParserProvider:
    """Groq if GROQ_API_KEY is set (preferred), else Claude if
    ANTHROPIC_API_KEY is set, else rules. Simulation and offline use never
    need a key."""
    if os.environ.get("GROQ_API_KEY"):
        return GroqGoalParser()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeGoalParser()
    return RuleBasedGoalParser()


@dataclass
class GoalParseResult:
    request: MissionRequest
    provider: str  # which parser actually produced the result, incl. any fallback


def parse_goal(
    text: str,
    available_vehicle_ids: list[str],
    search_area: SearchArea | None,
    default_altitude_m: float = 20.0,
    provider: GoalParserProvider | None = None,
) -> GoalParseResult:
    """Turn operator text into a validated MissionRequest, or raise
    NeedsClarification with the question to ask instead."""
    text = text.strip()
    if not text:
        raise NeedsClarification(UNCLEAR_QUESTION)
    if OUT_OF_SCOPE_PATTERN.search(text):
        raise NeedsClarification(
            "That's outside what this system does. It only plans simulated search, survey and "
            "inspection missions -- detections are shown to you, nothing else. "
            "What area would you like searched?"
        )

    provider = provider or default_provider()
    provider_name = provider.name
    try:
        parsed = provider.parse(text)
    except ProviderUnavailable as exc:
        fallback = RuleBasedGoalParser()
        provider_name = f"{fallback.name} (fallback: {provider.name} unavailable -- {exc})"
        try:
            parsed = fallback.parse(text)
        except MalformedProviderOutput:
            raise NeedsClarification(
                "I couldn't read the numbers in that request. Could you rephrase it, e.g. "
                "'search the area with 3 drones at 30 m'?"
            )
    except MalformedProviderOutput:
        raise NeedsClarification(
            "I couldn't reliably interpret that request. Could you rephrase it, e.g. "
            "'search the area with 3 drones at 30 m'?"
        )

    if parsed.intent != "area_search":
        raise NeedsClarification(parsed.clarifying_question or UNCLEAR_QUESTION)

    if search_area is None:
        raise NeedsClarification(
            "There's no search area yet. Draw a geofence (or search area) on the map first, "
            "then ask again."
        )

    available = sorted(available_vehicle_ids)
    if not available:
        raise NeedsClarification("No drones are available right now. Start the fleet, then ask again.")
    num = parsed.num_vehicles or len(available)
    if num > len(available):
        raise NeedsClarification(
            f"You asked for {num} drones, but only {len(available)} are available "
            f"({', '.join(available)}). Should I plan with {len(available)}?"
        )

    request = MissionRequest(
        goal_text=text,
        vehicle_ids=available[:num],
        search_area=search_area,
        altitude_m=parsed.altitude_m or default_altitude_m,
    )
    return GoalParseResult(request=request, provider=provider_name)
