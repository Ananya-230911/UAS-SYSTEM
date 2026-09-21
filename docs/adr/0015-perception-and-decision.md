# ADR-0015: Perception & AI Decision Service

## Status
Accepted

## Context
A reference "13-module UAS AI-driven autonomous mission system"
architecture was raised as a target for this project. That diagram
describes a much larger system than UAS-SYSTEM (real camera/LiDAR
hardware, an LLM-based agent with a vector database, edge deployment on
NVIDIA Jetson, a named cloud platform) — building it literally is out
of scope for this project's environment (no physical sensors, no edge
hardware) and would require throwing away or massively restructuring
everything built in Phases 1-6b, which is explicitly not what was
asked for. This ADR covers what was actually agreed: **software-only,
additive** versions of three pieces of that diagram — sensor
acquisition (camera/LiDAR), AI perception (object detection), and AI
decision-making (RAG + guardrails) — built as a new, separate service
alongside the existing ones, not a replacement for any of them.

## Decision

**A new, separate service: `services/perception/`.** Not folded into
`services/api`, for one concrete reason: its object-detection
dependency (`ultralytics`, which pulls in PyTorch) is a few hundred MB
and meaningfully slower to install than everything else in this
project combined. Forcing that onto anyone who just wants to run the
core GCS stack (telemetry, commands, missions, fleet, geofencing)
would be a real regression to the "run three/four lightweight Python
processes" design goal every phase up to now has protected. It's
started separately (`scripts/run_perception.ps1`), deliberately not
folded into `run_local.ps1`, so the core demo path stays exactly as
reliable as it already was.

**What's real vs. simulated — stated plainly, module by module:**

- **Camera (`app/camera.py`) — simulated.** Cycles through a small set
  of bundled still images per vehicle. There is no physical camera in
  this project (the same choice ADR-0001 already made for the vehicle
  itself).
- **Object detection (`app/detection.py`) — real.** An actual
  pretrained YOLOv8n model (Ultralytics), genuinely run via inference
  on each simulated frame — not a hardcoded or mocked response. This is
  the same model family real drones use for onboard detection; only the
  image it's fed is synthetic.
- **LiDAR (`app/lidar.py`) — simulated.** A synthetic range scan
  (36 beams, one per 10°), deterministic per (vehicle, tick) so it's
  testable, with an injected close-obstacle cluster on roughly 30% of
  ticks so obstacle detection has something to find.
- **Obstacle detection (`app/obstacle.py`) — real, simple.** Plain
  distance-threshold clustering over the (simulated) scan — genuine
  geometric processing, not machine learning. This is intentional:
  Modules 5/6 of the reference diagram (acquisition/preprocessing) are
  about data handling, not AI; the AI is specifically detection and
  decision-making.
- **AI decision (`app/decision.py`) — real, intentionally lightweight
  RAG.** TF-IDF retrieval (scikit-learn) over a small local text
  knowledge base of five safety guidelines stands in for
  retrieval-augmented generation, without a hosted LLM or vector
  database. This project has no other use for either, and a real,
  fully local, explainable retrieval step is a more honest claim than
  wiring up an LLM call nothing else here needs. Verified with a real
  retrieval test: an obstacle-plus-low-battery scenario genuinely
  retrieves the guideline that names both conditions, not a hardcoded
  branch.
- **Guardrails (`app/decision.py`) — real, independent rules.** Three
  hard checks (in-flight actions require armed, low battery overrides
  DIVERT/HOLD with RTL, a detected person can never result in
  CONTINUE) that run after retrieval and can override its raw
  suggestion. Verified live: a real request (person detected, battery
  15%) retrieved guideline G2 (→ HOLD) and the battery guardrail
  correctly overrode it to RTL — an actual guardrail intervention, not
  a scripted demo.

**Deliberately advisory-only**, the same principle
`services/api/app/risk_monitor.py` already established (ADR-0013):
`recommend_action()` never issues a command. It returns a
recommendation for a human operator to act on — the geofence failsafe
(ADR-0012) remains the only part of this system that autonomously
commands a vehicle, and this doesn't change that.

## Consequences
- `apps/gcs-web` gained a "Perception & AI Decision" panel: on-demand
  buttons for object detection and a recommendation, rendered readably
  (not raw JSON), reusing the fetch-on-demand pattern already
  established for Remote ID (ADR-0013).
- **Known limitation — this service has no authentication.** Unlike
  `services/api` (ADR-0011), there's no API key gate here yet. Adequate
  for local development; a **follow-up trigger, not built now**: add
  the same `require_api_key` pattern if this is ever deployed somewhere
  reachable beyond localhost.
- **Known limitation — the "camera" and "LiDAR" are synthetic, not a
  simulated physics/sensor model.** Unlike `simulation/sitl/sim.py`
  (a real MAVLink-speaking flight simulator with actual flight
  dynamics), this doesn't simulate optics or LiDAR physics — it's a
  small fixed image set and a seeded pseudo-random range generator.
  Sufficient to exercise genuine detection/decision logic end-to-end;
  not a claim of sensor-realistic simulation.
- **Known limitation — five guidelines is a demonstration knowledge
  base, not a curated safety corpus.** A real deployment would need
  many more guidelines, reviewed by an actual safety/regulatory
  process, not five hand-written sentences.
- **Known limitation — thresholds are demonstration constants**, the
  same caveat ADR-0013's risk monitor already carries: the obstacle
  range threshold, confidence threshold, and guardrail battery cutoff
  are reasonable-looking numbers, not values validated against real
  flight data.
- Does not touch Modules 9 (true autonomous multi-UAS coordination,
  beyond ADR-0009's existing multi-vehicle visibility), 11 (edge
  hardware), or 12 (cloud data platform) from the reference diagram —
  out of scope for the reasons in Context above.
