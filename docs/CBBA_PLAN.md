# CBBA Multi-Drone Task Allocation — Revised Plan (for approval)

Status: **APPROVED.** Phases A-F done (ADR-0018, ADR-0019, ADR-0020,
ADR-0021). F also gained the one-line survey the operator asked for:
search any place, type "3 drones survey this area", watch them talk, fly
and report camera results (ADR-0021). Remaining: G (scenario tests). One deviation found while building
Phase B: bid warping was added to the bundle phase (section 2A, Decide).

Simulation only. These are inspection scenarios: drones visit inspection
points and take pictures. Nothing in this plan arms, takes off, or commands
a real vehicle. Nothing in this plan selects, prioritises or engages
targets. That boundary is the same one `docs/IMPLEMENTATION_PLAN.md` §3
already sets.

---

## 0. Hard rules (unchanged from the original plan)

1. LLMs never compute, edit, or override bids, winners, bundles, or routes.
   CrewAI may only call the allocator through a tool and read its result.
2. Geofence, battery/endurance and route validation are deterministic code.
   They run after allocation and before anything is dispatched. A failed
   check blocks dispatch. An LLM never "explains it away".
3. Don't build a second copy of a component that already does the job.
   Reuse or extend it, and say so.
4. Don't claim real distributed consensus unless messages actually travel
   over a real transport between separate processes, and that is tested.
   In-process simulation is labelled as simulation.
5. Simulation and inspection scenarios only. No arming, takeoff or
   real-vehicle commands.

## 1. Decisions taken (from the Phase A questions)

| # | Decision | What it means in the repo |
|---|---|---|
| 1 | A separate, lightweight, in-process CBBA simulation inside UAS-SYSTEM | New package `services/fleet-agent/app/cbba/`. It never imports `pymavlink`, never calls the `services/api` endpoints `/commands` or `/missions`, and never talks to a telemetry gateway. Start positions may be **read** from the live fleet through the existing read-only `app/demo.py::load_live_fleet()`; otherwise a built-in scenario is used. |
| 2 | §2A/§2B/§2C were missing, so they are proposed below | See sections 2A, 2B and 2C. |
| 3 | Groq free tier as the preferred LLM provider | It is optional. With no key, everything works, using the existing rule-based parser. See section 6 for what was and wasn't verified. |
| 4 | In-memory storage first, SQLite later | A `MissionStore` Protocol plus an `InMemoryMissionStore`. A `SqliteMissionStore` can be added later without touching callers. |

---

## 2A. The agent loop (proposed)

Every simulated drone is one `DroneAgent` object. It runs the same loop on
every simulation tick:

```
observe  ->  communicate  ->  decide  ->  act  ->  react
```

**Observe — what a drone knows.** It sees only its own state plus what it
has been told:
- its own position, battery, speed, capabilities, health (`OK / DEGRADED /
  OFFLINE`) and the points it has completed;
- the inspection points published for the mission (id, position,
  required capability, time window, value);
- the last CBBA message from each neighbour it can currently hear.

It never reads another drone's internal state directly. That is the whole
point of the experiment.

**Communicate.** It sends its CBBA vectors to each drone that is currently
in range on the comms graph:
- `y`: the winning bids it knows about;
- `z`: who it thinks won each point;
- `s`: when it last heard from each drone.

The messages go through a `Transport` interface (see section 4). In
version 1 this is an in-memory queue, labelled `CBBA-sync-sim`.

**Decide.** This is pure CBBA (Choi, Brunet & How 2009):
1. *Bundle phase* (Algorithm 3). The drone adds the point that raises its
   score the most, given the route it already has. It keeps doing this
   until its bundle is full, or until nothing it can still fly is left.
   The score is a time-discounted reward: `λ^arrival_time × value`.
   "Can still fly" uses the same endurance maths as
   `app/safety/validator.py`.
2. *Consensus phase* (Table I). For each point it compares its own view
   with what neighbours sent, and updates the point's winner. It drops a
   point it was outbid on, plus everything it added after that point
   (eq. 6).
3. Ties always break the same way: higher bid wins, then lower drone id,
   then lower point id. The same inputs always give the same answer.
4. *Bid warping* (added in Phase B, see ADR-0018). With travel time, a
   point can be worth MORE after a nearby point is added, which breaks
   the paper's diminishing-gain assumption and made drones trade a point
   back and forth forever in testing. A drone still picks by true gain,
   but the bid it publishes is capped at its earlier bids (Johnson, Choi,
   Ponda & How, CDC 2012), which restores convergence.

**Act.** CBBA only produces a plan. It moves nothing.
- The allocation is final only when two things hold: no drone's
  winners changed for `D` rounds (`D` = comms-graph diameter), and the
  deterministic safety gate passes (section 3).
- After that, a drone's kinematic model in `app/sim/` "flies" toward its
  next point: a straight line at cruise speed, with battery draining at
  the same rate as `SafetyConstraints`.
- When it is within a small radius of a point, that point is marked
  complete.

**React.** Things that change what a drone should do are turned into
events by deterministic code:

| Event | Detected by | Reaction |
|---|---|---|
| No heartbeat from a drone for `T` seconds | Each neighbour and the supervisor (timer) | Mark that drone `OFFLINE`. Its uncompleted points are released (winner cleared). A replan request is raised. |
| Battery projected below reserve before the bundle is done | The drone itself (validator maths) | It drops the points it can no longer finish, then rebids. If it can't make it home, it requests return-home (in the sim). |
| A link breaks or a partition forms (comms graph changes) | `Transport` / network model | CBBA keeps running on each connected part. When the parts reconnect, consensus merges them. Timestamps (eq. 5) decide whose information is fresher. |
| An inspection point is added or cancelled | Supervisor | A replan request is raised. |
| The geofence changes | Supervisor | All routes are re-validated. Anything now invalid is blocked and a replan is requested. |

---

## 2B. CrewAI supervision and replanning (proposed)

CrewAI sits **above** the simulation and **never inside it**. It reads
reports and asks for things. Deterministic code decides whether a request
is allowed, and carries it out.

**What CrewAI is allowed to ask for — a fixed list of "intents":**

| Intent | Who can raise it | What runs |
|---|---|---|
| `RequestReplan(reason)` | CrewAI, operator, or a deterministic rule | Re-runs CBBA from the current state. |
| `MarkPointPriority(point_id, value)` | CrewAI (suggestion) / operator | Changes the point's value. It is checked against `[min, max]`. CBBA is then re-run. |
| `ExcludeDrone(drone_id, reason)` | CrewAI (suggestion) / operator | That drone stops bidding. Its points are released and CBBA is re-run. |
| `Summarise()` | anyone | Read-only. A plain-English report built from the stored results. |

Every intent is a Pydantic model. It goes through an `IntentGate` before
it can do anything. The gate is deterministic Python:
- it checks the schema;
- it checks that the ids exist;
- it checks values are within limits;
- it checks the request rate (no replan storms);
- it refuses anything outside the list.

CrewAI's output is **only** a list of intents. It cannot contain bids,
winners, bundles, routes, or waypoints. If the LLM returns anything else,
that output is discarded and logged.

**The CrewAI tools (thin wrappers around deterministic code):**
- `GetMissionStateTool`: read-only snapshot from the `MissionStore`.
- `RunAllocatorTool`: calls `cbba.service.allocate(...)` and returns its
  result. The LLM sees the result; it can't change it.
- `ValidatePlanTool`: calls the safety gate and returns pass/fail plus
  the issues.
- `RequestReplanTool`, `MarkPointPriorityTool`, `ExcludeDroneTool`: each
  one only *submits an intent* to the `IntentGate`.

**Replanning flow (the same path whoever asks):**

```
trigger (rule / operator / CrewAI intent)
   -> IntentGate (deterministic accept/reject, logged)
   -> CBBA re-run from current state (completed points stay completed)
   -> safety gate (section 3)  --fail-->  blocked, reason shown, old plan
   |                                      keeps running or drones hold
   -> pass -> new plan version stored (hash), dashboard updated
```

**Without Groq / without CrewAI:** none of this is required. Deterministic
rules raise replans on drone loss, low battery and fence changes. The
operator can press "Replan" in the dashboard. CrewAI is an optional extra
layer that suggests intents and writes summaries.

---

## 2C. Future computer-vision interface (proposed — interface only, not built)

The goal is that later a drone's camera can say "I saw something at this
inspection point". That observation becomes a **report**, stored for a
human to look at. It never becomes an action.

```python
class InspectionObservation(BaseModel):      # display/logging data only
    point_id: str
    drone_id: str
    observed_at: datetime
    labels: list[DetectedLabel]               # class_name + confidence (0..1)
    image_ref: str | None                     # path/URL, never raw bytes in messages
    source: Literal["simulated", "perception-service"]

class ObservationSource(Protocol):
    def observe(self, drone_id: str, point_id: str) -> InspectionObservation | None: ...
```

- **Version 1** ships only `SimulatedObservationSource`. It returns a
  fixed, seeded result, or nothing.
- **Later** a `PerceptionServiceSource` calls the existing
  `services/perception` HTTP API, which already wraps YOLOv8n in
  `app/detection.py::detect_objects` and the webcam in `app/camera.py`.
  This reuses that service. It does not build a second detector.
- Observations are stored and shown on the dashboard. The only thing an
  observation can do is let the *operator* add a "re-inspect this point"
  request. That request goes through the normal `IntentGate`.
- It reuses the shape of the existing `Detection` model in
  `app/core/models.py`. That model is already documented as
  "display/logging data only — no targeting/engagement logic".

---

## 3. Deterministic safety gate (rule 2)

Every CBBA result (initial, replan, after a failure) passes through
`cbba/safety_gate.py` before any simulated drone may follow it. It
**reuses** `app/safety/validator.py` and `app/safety/geofence.py`; it does
not rewrite them:

- every inspection point and every route segment is inside the geofence
  (`segment_crosses_fence_boundary`, which already handles concave
  fences);
- every drone's start point is inside the fence;
- altitude is within limits (`check_altitude`);
- endurance plus return home leaves the battery reserve
  (`check_endurance`);
- the drone has the capability each assigned point requires (new — a
  small check);
- no point is assigned to two drones (new — CBBA conflict-free check);
- every drone named in the result is known and not `OFFLINE`.

If any check fails, the plan is **blocked**. The reason is stored and
shown. Neither CrewAI nor the operator can override a failed check. The
only fix is to change the inputs and replan.

---

## 4. How agents exchange information

```python
class CbbaMessage(BaseModel):
    sender_id: str
    round: int
    winning_bids: dict[str, float]      # y
    winners: dict[str, str | None]      # z
    timestamps: dict[str, float]        # s (eq. 5)

class Transport(Protocol):
    def send(self, msg: CbbaMessage, to: str) -> None: ...
    def receive(self, drone_id: str) -> list[CbbaMessage]: ...
```

- **v1: `InMemoryTransport`.** This is a queue per drone. It can drop
  messages, add delay, or cut links, following a seeded `CommsGraph`
  (range-based or fixed). It is labelled **"CBBA-sync-sim
  (in-process)"** everywhere, including the API, the dashboard and the
  docs.
- **Later (optional):** a transport between separate processes (e.g.
  local UDP). "Distributed consensus" is claimed only once that exists
  **and** a test proves it (rule 4).
- The message shape reuses the existing `AgentMessage` model
  (`sender_id`, `recipient_id`, `payload`, `sequence`). `CbbaMessage` is
  the typed payload.

## 5. Integration points in the actual repository

| Need | Where | New / reuse |
|---|---|---|
| CBBA models (inspection point, drone, capability, result) | `services/fleet-agent/app/cbba/models.py` | New. It reuses `Waypoint`, `Geofence` and `SafetyConstraints` from `app/core/models.py`. |
| Distance / geometry | `app/cbba/` imports `app/safety/geofence.py::horizontal_distance_m` | Reuse. |
| Score (time-discounted reward) | `app/cbba/scoring.py` | New. |
| Bundle phase, consensus (Table I) | `app/cbba/agent.py`, `app/cbba/consensus.py` | New. |
| SGA reference allocator (oracle for tests) | `app/cbba/sga.py` | New. The existing `planning/allocation.py` auction is a *coverage-cell* auction with a different cost, so it is **not** an SGA oracle. It stays as it is, untouched. |
| Comms graph + transport | `app/cbba/network.py` | New (in-memory). |
| Rounds until converged | `app/cbba/runner.py` | New. |
| Safety gate | `app/cbba/safety_gate.py` → `app/safety/validator.py` | Reuse plus two small new checks. |
| Public entry point | `app/cbba/service.py::allocate(scenario) -> CbbaResult` | New. This is the only thing CrewAI's tool calls. |
| Storage | `app/store.py`: `MissionStore` Protocol + `InMemoryMissionStore` | New. SQLite comes later, behind the same Protocol. |
| Read live start positions (read-only) | `app/demo.py::load_live_fleet()` | Reuse. It only `GET`s `/vehicles`, `/vehicles/{id}/telemetry` and `/geofence`; skips stale or incomplete drones. If the API isn't running, the built-in scenario is used. |
| Kinematic sim drones + agent loop | `app/sim/drone.py`, `app/sim/world.py` | New. No MAVLink; positions are just numbers (rule 5). |
| Intents + gate | `app/supervisor/intents.py`, `app/supervisor/gate.py` | New. |
| CrewAI crew + tools | `app/crew/` + `requirements-crew.txt` | New, optional. `import crewai` happens lazily. Core tests never need it. |
| Groq goal parsing | `app/mission/goal_parser.py`: add `GroqGoalParser` beside `ClaudeGoalParser` | Extends the existing parser. Same Protocol, same out-of-scope refusal, same fall back to `RuleBasedGoalParser`. |
| HTTP API for dashboard | `services/fleet-agent/app/server.py` (FastAPI, port **8020**) | New. `services/api` is **not** touched. |
| Dashboard | `apps/gcs-web`: new "CBBA simulation" sidebar section + per-drone Leaflet layers | Extends the existing page. It talks to `:8020` only. |
| Tests | `services/fleet-agent/tests/cbba/` | New; added to the existing CI job. |
| Env vars | `services/fleet-agent/.env.example` gains `GROQ_API_KEY=`, `GROQ_MODEL=` | Extends the file. No secrets are committed. |

## 6. Groq — what was checked (October 2026)

**Verified directly:**
- **`crewai` 1.15.27** (latest on PyPI) requires Python `>=3.10,<3.14`.
  Your Python 3.13 is fine.
- I downloaded the CrewAI wheel and read its source. **Groq is not a
  native CrewAI provider.**
  - The native ones are OpenAI, Anthropic, Azure, Bedrock, Gemini, plus
    an OpenAI-compatible list (OpenRouter, DeepSeek, Ollama, vLLM,
    Cerebras, DashScope, DeepInfra). Groq is not on that list.
  - So Groq works one of two ways: (a) `pip install "crewai[litellm]"`
    and `LLM(model="groq/<model-id>")`; or (b) the native OpenAI provider
    pointed at Groq's OpenAI-compatible endpoint (`custom_openai=True,
    base_url="https://api.groq.com/openai/v1"`).
  - The plan uses **(b)**, so it doesn't pull in LiteLLM.
  - A trap I found in the source: with `custom_openai=True`, CrewAI
    strips a leading `openai/` from the model name. That would turn
    Groq's model id `openai/gpt-oss-20b` into `gpt-oss-20b`. Phase D must
    handle this and test it.
- The official `groq` Python SDK is 1.7.0 (requires Python `>=3.10`).

**Not verified (this sandbox's network policy blocks
`api.groq.com` and `console.groq.com`):**
- **Current model list.** Search results report two things:
  - Groq shut down `llama-3.3-70b-versatile` and `llama-3.1-8b-instant`
    for free/developer tiers on 16 Aug 2026.
  - `openai/gpt-oss-20b` / `openai/gpt-oss-120b` are the production
    replacements.
  - So the plan defaults to `openai/gpt-oss-20b`. It is configurable via
    `GROQ_MODEL`, never hard-coded in logic.
- **Free-tier limits.** Third-party sources say about 30 requests/min per
  model, with per-minute token and per-day request caps that vary by
  model. The limits apply per organisation, not per key. The real numbers
  are on your Groq console's limits page.
  - This hardly matters here: one goal parse plus a few supervisor
    summaries per mission.
  - A `429` simply falls back to rules.
- **A real successful Groq call.** You will need to run one on your
  machine once with your own key. The plan includes a one-line check
  command for that.

**How Groq is used:**
- **Goal parsing.** Groq returns JSON, which is validated against the
  existing `ParsedGoal` schema. Bad JSON leads to a question for the
  operator. Network error, `401` or `429` falls back to rules.
- **CrewAI supervisor.** It only produces intents and summaries
  (section 2B).
- **Never** in allocation, safety checks or dispatch.
- With **no** `GROQ_API_KEY`, every phase works and every test passes.

## 7. Phases (unchanged order, now with exact paths)

- **B — CBBA core** (`app/cbba/`): models, scoring, SGA oracle, bundle,
  consensus, in-memory network, runner, safety gate, `allocate()`.
- **C — Tests** (`tests/cbba/`):
  - determinism;
  - conflict-free;
  - keeps at least 50% of the SGA score (exact SGA match is measured,
    not guaranteed -- see ADR-0018);
  - converges within `N·D` rounds;
  - consensus table cases;
  - lost drone → reallocated;
  - a partition then a merge;
  - fence or battery failure → blocked;
  - capability mismatch never assigned.
- **Moved forward (done after C, at the operator's request):** a read-only
  first slice of D's server (`app/server.py`, port 8020) and of F's map panel
  (`apps/gcs-web/cbba.js`), so the CBBA result can be seen in the GCS UI.
  See ADR-0018, "Map preview".
- **D — Backend + supervisor:**
  - `MissionStore` (in-memory);
  - `server.py` on :8020;
  - `IntentGate`;
  - `GroqGoalParser`;
  - optional `app/crew/` + `requirements-crew.txt`;
  - tests with fake LLM clients.
- **E — Sim agents:** `app/sim/` agent loop over `InMemoryTransport`,
  heartbeats, failure injection, read-only live start positions.
- **F — Dashboard:** gcs-web "CBBA simulation" section; inspection
  points, per-drone bundle routes, winners, convergence rounds, blocked
  reasons, the "sim" label. **Done**, plus the one-line survey
  (`app/survey.py`, `/survey/plan`, `/survey/camera`; ADR-0021), which
  also builds a first, display-only version of §2C (camera results from
  the perception service: a webcam picture taken when the drone reaches
  the place + YOLOv8, or "camera not connected"; not stored yet).
- **G — Scenario tests:** end-to-end scripted scenarios (nominal, drone
  loss, comms cut, low battery, fence change), with real run output
  reported.

After each phase: run all suites, report the real numbers, then stop for
approval.

## 8. Out of scope

- Real vehicles, arming, takeoff, or any MAVLink command from `app/cbba/`
  or `app/sim/`.
- Any target selection, attack, strike, engagement or sensor-to-shooter
  logic. Inspection points are places to look at, nothing else.
- Claims of real distributed consensus before an inter-process transport
  exists and is tested.
