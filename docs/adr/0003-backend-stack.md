# ADR-0003: Backend Stack

## Status
Accepted

## Context
Need a language/framework for the telemetry gateway (talks MAVLink, needs a
background receive loop) and the API service (REST + WebSocket, persistence).

## Decision
**Python + FastAPI** for both services.

- `pymavlink` (the reference MAVLink implementation) is Python-first; using
  the same language for the gateway avoids a cross-language MAVLink binding.
- FastAPI gives async request handling and native WebSocket support with
  minimal boilerplate, and Pydantic models double as request/response
  validation and (lightly) as API documentation (`/docs`).
- Using FastAPI for *both* services (rather than Python for the gateway and
  Node for the API, as sketched as an alternative in the Phase 1 plan) keeps
  Phase 1's two-service footprint to one toolchain, one dependency-management
  approach, and one test framework (`pytest`) — the API/gateway split is a
  service boundary, not a language boundary, and can diverge later if a
  concrete reason shows up.

## Consequences
- One requirements file style, one linter config, one test runner across
  both backend services.
- The gateway and API are still separate deployable processes communicating
  over HTTP (`services/telemetry-gateway` → `POST /internal/telemetry` on
  the API; API → `POST /command` on the gateway), matching the architecture
  in the Phase 1 plan — this is a process/service boundary decision, not a
  language one, so it does not conflict with ADR-0006 (in-process pub/sub
  *within* a service).
