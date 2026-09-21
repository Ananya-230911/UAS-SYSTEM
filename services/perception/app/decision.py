"""AI decision & planning: retrieval-augmented recommendation +
guardrails (docs/adr/0015-perception-and-decision.md).

A deliberately lightweight but real implementation of the pattern: TF-IDF
retrieval over a small local text knowledge base stands in for RAG
(retrieval-augmented generation) without a hosted LLM or vector
database -- this project has no need for either at its scale, and a
real, explainable, fully-local retrieval step is a more honest claim
than wiring up an LLM call this project doesn't otherwise use anywhere.
The guardrail checks below are real, independently-evaluated rules, not
a restatement of the retrieval result.

Deliberately advisory-only, the same principle app/risk_monitor.py
already established in services/api: recommend_action() never issues a
command. It returns a recommendation for a human operator to act on.
"""
from dataclasses import dataclass, field

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# A small local safety-guideline knowledge base. In a real system this
# would be a much larger, curated document set; five guidelines is
# enough to demonstrate genuine retrieval (different situations really
# do match different guidelines) without pretending to be exhaustive.
GUIDELINES = [
    {
        "id": "G1",
        "text": "If an obstacle is detected within a few meters of the vehicle, hold position or divert around it before continuing the mission.",
    },
    {
        "id": "G2",
        "text": "If a person is detected in or near the flight path, pause the mission until the person is clear or an operator confirms it is safe to proceed.",
    },
    {
        "id": "G3",
        "text": "A degraded GPS fix combined with any detected obstacle should be treated as high risk; prefer returning to launch over continuing the mission.",
    },
    {
        "id": "G4",
        "text": "Low battery combined with any detected obstacle should prioritize an immediate return to launch over further investigation.",
    },
    {
        "id": "G5",
        "text": "Routine detections of stationary objects such as parked vehicles or structures well outside the flight path do not require any action.",
    },
]

_vectorizer = TfidfVectorizer()
_guideline_matrix = _vectorizer.fit_transform([g["text"] for g in GUIDELINES])

_ACTION_FOR_GUIDELINE = {"G1": "DIVERT", "G2": "HOLD", "G3": "RTL", "G4": "RTL", "G5": "CONTINUE"}


def _retrieve_guideline(query: str) -> dict:
    query_vec = _vectorizer.transform([query])
    similarities = cosine_similarity(query_vec, _guideline_matrix)[0]
    best_idx = int(similarities.argmax())
    return {**GUIDELINES[best_idx], "similarity": round(float(similarities[best_idx]), 3)}


def _situation_query(
    detections: list[dict],
    obstacles: list[dict],
    battery_pct: float | None,
    gps_fix_type: int | None,
) -> str:
    parts = []
    if obstacles:
        parts.append(f"obstacle detected {min(o['range_m'] for o in obstacles):.1f} meters away")
    if any(d["class_name"] == "person" for d in detections):
        parts.append("person detected in flight path")
    if gps_fix_type is not None and gps_fix_type < 3:
        parts.append("degraded gps fix quality")
    if battery_pct is not None and battery_pct < 25:
        parts.append("low battery remaining")
    if not parts:
        parts.append("routine detection of a stationary object well clear of the flight path")
    return " ".join(parts)


@dataclass
class Recommendation:
    action: str  # CONTINUE / HOLD / DIVERT / RTL
    reasoning: str
    matched_guideline: dict
    guardrail_checks: list[dict] = field(default_factory=list)


def recommend_action(
    detections: list[dict],
    obstacles: list[dict],
    armed: bool,
    battery_pct: float | None = None,
    gps_fix_type: int | None = None,
) -> Recommendation:
    query = _situation_query(detections, obstacles, battery_pct, gps_fix_type)
    guideline = _retrieve_guideline(query)
    raw_action = _ACTION_FOR_GUIDELINE[guideline["id"]]
    action = raw_action
    checks = []

    # Guardrail 1: nothing is actionable in flight if the vehicle isn't
    # flying -- a disarmed vehicle can't meaningfully HOLD/DIVERT/RTL.
    armed_check = {"rule": "in-flight actions only apply while armed", "passed": True}
    if not armed and action != "CONTINUE":
        armed_check["passed"] = False
        armed_check["note"] = "vehicle is disarmed -- no in-flight action needed"
        action = "CONTINUE"
    checks.append(armed_check)

    # Guardrail 2: battery safety always outranks a DIVERT/HOLD -- if
    # you're low on power, go home, don't linger investigating something.
    battery_check = {"rule": "low battery overrides DIVERT/HOLD with RTL", "passed": True}
    if battery_pct is not None and battery_pct < 25 and action in ("DIVERT", "HOLD"):
        battery_check["passed"] = False
        battery_check["note"] = f"battery at {battery_pct}% overrides {action} -> RTL"
        action = "RTL"
    checks.append(battery_check)

    # Guardrail 3: never wave a mission through with a person still
    # detected in-path -- if retrieval alone said CONTINUE, escalate.
    person_check = {"rule": "never CONTINUE with a person detected in-path", "passed": True}
    if any(d["class_name"] == "person" for d in detections) and action == "CONTINUE":
        person_check["passed"] = False
        person_check["note"] = "person detected -- escalating CONTINUE to HOLD"
        action = "HOLD"
    checks.append(person_check)

    reasoning = f'Matched guideline {guideline["id"]} (similarity {guideline["similarity"]}): "{guideline["text"]}"'
    if action != raw_action:
        reasoning += f" Guardrails adjusted the recommendation from {raw_action} to {action}."

    return Recommendation(action=action, reasoning=reasoning, matched_guideline=guideline, guardrail_checks=checks)
