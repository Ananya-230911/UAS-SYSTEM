# ADR-0019: CBBA Phase D — Instructions (Groq / CrewAI), Intent Gate, Mission Store

## Status
Accepted (Phase D of `docs/CBBA_PLAN.md`)

## Context
The operator wants to type what should change ("P3 is high priority, D2
is offline") and have an AI help. The plan's hard rules still apply:
- an LLM never computes, edits or overrides bids, winners, bundles or
  routes;
- safety checks are deterministic and cannot be overridden;
- everything must work without an API key;
- Groq's free tier is the preferred provider (operator decision 3);
- storage is in memory first, behind an interface so SQLite can be added
  later (decision 4).

## Decision

**Intents** (`app/supervisor/intents.py`). The *only* things any
interpreter may request:

| Intent | Effect |
|---|---|
| `set_priority` | low / normal / high = CBBA value 50 / 100 / 150 |
| `require_sensor` | camera / thermal / lidar / any |
| `set_deadline` | 1 s .. 2 h, or remove |
| `skip_point` | leave a place out of this mission |
| `exclude_drone` / `include_drone` | take a drone out, or bring it back |
| `replan` | re-run CBBA |

No intent can carry a coordinate, a bid, a winner, a bundle or a route.
Anything else fails Pydantic validation, so it never reaches the gate.

**IntentGate** (`app/supervisor/gate.py`). Deterministic accept/reject of
each intent:
- the named place or drone must exist (case-insensitive, rewritten to
  the real id);
- values must be in range;
- at least one drone and one place must remain;
- at most 10 changes per instruction;
- at most 6 instructions a minute.

It produces a new scenario. CBBA then re-plans, and the CBBA safety gate
(ADR-0018) still decides dispatch.

**Interpreters** (`app/supervisor/instructions.py`), all behind one
interface:
- **Rule-based** (default, offline):
  - keywords plus the mission's own names;
  - short names work ("P03" for "P03-tower");
  - "2 minutes" becomes 120 s;
  - one sentence can carry several changes ("P3 and P5 are high
    priority, P2 needs thermal").
- **Groq** (`app/llm/groq.py`), used when `GROQ_API_KEY` is set:
  - a plain HTTPS call through httpx to Groq's OpenAI-compatible
    endpoint, in JSON mode, at temperature 0;
  - the answer must validate as `IntentBatch`;
  - no key, a network error, or HTTP 401 / 403 / 404 / 429 / 5xx all mean
    the rule-based parser takes over, and the result says so;
  - an answer that doesn't validate becomes a question for the operator,
    never a guess;
  - the model is set by `GROQ_MODEL`, default `openai/gpt-oss-20b`,
    because the Llama 3.x free-tier models were retired on 16 Aug 2026.
- **CrewAI** (`app/crew/supervisor.py`, optional, installed via
  `requirements-crew.txt`):
  - one agent with three tools: `get_mission_state` (read-only),
    `run_allocator` (read-only CBBA result), and `request_change` (records
    one intent; does not apply it);
  - the crew's output is only the recorded intents; its free-text answer
    is ignored;
  - Groq is used through CrewAI's native OpenAI provider (no LiteLLM).
    CrewAI strips a leading `openai/` from the model name, so one extra
    `openai/` is prefixed and Groq receives e.g. `openai/gpt-oss-20b`
    intact.
- **The same rule for every interpreter.** Out-of-scope text (weapons,
  attacks, targeting, payloads, "destroy", "enemy", "hostile") is refused
  *before* any interpreter runs.
- Groq's goal parsing for Phase 3 is also added: `GroqGoalParser` sits
  beside `ClaudeGoalParser`. The default is Groq, then Claude, then rules.

**Mission store** (`app/store.py`).
- `MissionStore` is a Protocol; `InMemoryMissionStore` is version 1:
  thread-safe, capped at 200 missions, oldest dropped first.
- Every plan is a new **version** of a mission: the first plan, each
  re-plan after an instruction, and each re-plan after a drone failure.
- Each version stores the inputs, the CBBA result, a SHA-256 content
  hash, the source, and the instruction text plus the accepted changes.

**Server** (`app/server.py`):
- `POST /missions/plan` (plan + save);
- `GET /missions`, `GET /missions/{id}`;
- `POST /cbba/instruct`, which returns a *proposal*: accepted changes,
  refused changes with reasons, and the changed scenario. Nothing is
  planned or saved until the operator applies it;
- `GET /ai/status`, which reports whether a key or CrewAI is present and
  never reveals the key.

There are still no command endpoints, and a test enforces it.

**GCS panel.** A "Tell the drones" box with an engine choice:
- auto (Groq if set up, else rules), rules only, or CrewAI;
- "Understand" shows the checked proposal;
- "Apply and re-plan" applies it and saves a new version.

Every plan shows "Saved as mission M-…, version n".

## How this was verified
- **fleet-agent tests: 473 passed, 1 skipped** (CrewAI not installed).
  The new ones cover:
  - the Groq client against a fake HTTP server (`httpx.MockTransport`):
    request shape, key, model, JSON mode, every error class, and
    malformed answers;
  - Groq-to-rules fallback, for both goals and instructions;
  - Groq trying to smuggle in a `set_route`;
  - out-of-scope text refused before Groq is called;
  - the rule parser;
  - every gate rule, including the rate limit with a fake clock;
  - an end-to-end instruction changing the plan through CBBA and safety;
  - the store and the API.
- **With CrewAI 1.15.27 installed** (separate venv): all 49 supervisor
  tests pass, including 5 that run the real CrewAI agent/tool loop with a
  scripted stand-in LLM. Results:
  - the agent's tools produced exactly the requested intents;
  - `set_route` and an invalid priority were refused at the tool;
  - an unknown place requested by the crew was refused by the gate;
  - the Groq model id reached the LLM unchanged.
- **JS tests: 21 passed.**
- **Headless-Chromium run:**
  1. The first plan was saved as version 1.
  2. "attack the tower" was refused.
  3. "P10 can be done by any drone, P03 and P05 are high priority, sim-2
     is offline" gave 4 accepted changes.
  4. Apply re-planned it: P10 is now inspected, sim-2 is out, PASSED,
     saved as version 2 with the instruction and changes.
  5. `/missions` showed both versions. No page errors.
- **NOT verified:** a real Groq call. `api.groq.com` and
  `console.groq.com` are blocked by this environment's network policy,
  so a successful live call (and the exact JSON-mode behaviour of the
  current models) must be checked on the operator's machine with their
  own key.

## Consequences
- Storage is lost when the server restarts. SQLite is a drop-in later.
- The rule parser understands common phrasings, not arbitrary language;
  that is what Groq is for.
- The rate limit is global to the server process, which is fine for one
  local operator.
- CI does not install CrewAI (it is large); its tests skip there.
