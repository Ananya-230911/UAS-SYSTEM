# UAS-SYSTEM — Phase 1 Detailed Plan

**System:** Unmanned Aerial System (UAS) — ground control, telemetry, and mission
management software stack for one or more drones.

**Phase:** 1 — Foundation & Architecture
**Status:** Draft
**Owner:** am4921@srmist.edu.in

---

## 1. Objective

Phase 1 establishes the technical foundation the rest of the project builds on:
a working architecture, a chosen tech stack, a scaffolded repo, and a thin but
real "hello drone" vertical slice — a simulated aircraft connected to a ground
control station over the standard telemetry protocol, with live telemetry
flowing into a backend and rendered on a basic UI.

Nothing in Phase 1 targets real flight hardware. The goal is to prove the
architecture end-to-end against a **simulator**, so every later phase (real
autopilot link, multi-drone fleet, video, mission autonomy) has a stable base
to extend instead of a redesign.

### Success criteria (exit checklist)
- [ ] Repo scaffolded with agreed structure, linting, and CI green on every push
- [ ] A SITL (software-in-the-loop) simulated drone boots in CI and locally
- [ ] Backend connects to the simulated drone over MAVLink and ingests telemetry
      (position, attitude, battery, GPS fix, arm state)
- [ ] Telemetry is persisted to a time-series-capable store and exposed over a
      backend API (REST + a streaming channel)
- [ ] Minimal web UI shows the drone on a map with live-updating telemetry
- [ ] Basic command path proven: UI/backend can arm/disarm and send a simple
      command (e.g., takeoff in simulation) and see the state change reflected
- [ ] Architecture Decision Records (ADRs) written for the big choices below
- [ ] Dev environment reproducible by a new contributor in under 15 minutes
      (documented setup, one command to run the whole stack locally)

---

## 2. Scope

**In scope for Phase 1:**
- Architecture design and ADRs
- Repo scaffolding (monorepo layout, tooling, CI)
- SITL simulator integration (single simulated vehicle)
- Telemetry ingestion service (MAVLink → internal event bus → storage)
- Minimal backend API (REST + WebSocket/SSE for live telemetry)
- Minimal GCS web UI: live map, telemetry readout, arm/disarm + takeoff command
- Core data models (Vehicle, TelemetrySample, Mission stub, Command/Event log)
- Local dev environment (Docker Compose) + CI pipeline

**Explicitly out of scope for Phase 1** (later phases):
- Real flight-controller hardware / radio link
- Multi-drone fleet management and swarm coordination
- Full mission planning (waypoint editor, geofencing enforcement, RTL logic)
- Video/sensor payload streaming
- Authentication/authorization hardening, multi-tenant orgs
- Regulatory/compliance features (remote ID, airspace deconfliction)
- Production deployment/infra (Phase 1 is local + CI only)

---

## 3. Architecture Overview

```
┌─────────────────┐        MAVLink/UDP        ┌───────────────────────┐
│  SITL Simulator  │ ◄────────────────────────► │  Telemetry Gateway    │
│ (PX4 or ArduPilot│                             │  (MAVLink client,     │
│  SITL, Gazebo    │                             │  Python/mavsdk)       │
│  optional)       │                             └───────────┬───────────┘
└─────────────────┘                                          │ internal events
                                                               ▼
                                                   ┌───────────────────────┐
                                                   │   Backend API Service │
                                                   │  (REST + WS/SSE)      │
                                                   │  - command dispatch   │
                                                   │  - telemetry store    │
                                                   └───────────┬───────────┘
                                                               │
                                        ┌──────────────────────┼───────────────────┐
                                        ▼                                          ▼
                              ┌──────────────────┐                     ┌──────────────────┐
                              │  Time-series DB   │                     │   Web UI (GCS)    │
                              │ (telemetry history)│                     │ live map + status │
                              └──────────────────┘                     └──────────────────┘
```

**Key architectural decisions to formalize as ADRs during Phase 1:**

| # | Decision | Recommendation | Rationale |
|---|----------|-----------------|-----------|
| ADR-1 | Autopilot/simulator | PX4 SITL + MAVSDK | Most active open-source ecosystem, good Python/JS SDKs, Gazebo integration path for later phases |
| ADR-2 | Telemetry protocol | MAVLink v2 | Industry standard for UAS, supported by both PX4 and ArduPilot, well-documented |
| ADR-3 | Backend language/runtime | Python (FastAPI) for telemetry gateway; Node/TypeScript (or same FastAPI) for API | MAVSDK/pymavlink has first-class Python support; FastAPI gives async WS support out of the box |
| ADR-4 | Telemetry storage | TimescaleDB (Postgres extension) or InfluxDB | Purpose-built for high-frequency time-series writes and range queries |
| ADR-5 | Frontend | React + TypeScript + a mapping lib (MapLibre GL or Leaflet) | Widely supported, good real-time map/marker update patterns |
| ADR-6 | Internal messaging | In-process pub/sub for Phase 1 (upgrade to Redis/NATS in a later phase if multi-service fan-out is needed) | Avoid infra overhead until multi-drone/multi-service actually requires it |
| ADR-7 | Monorepo layout | Single repo, `services/*` + `apps/*` structure | Small team, tight coupling between gateway and API in early phases |

Each ADR gets a short `docs/adr/000X-title.md` file: context, decision, alternatives considered, consequences.

---

## 4. Proposed Repo Structure

```
UAS-SYSTEM/
├── docs/
│   ├── PHASE_1_PLAN.md          (this file)
│   └── adr/                     architecture decision records
├── services/
│   ├── telemetry-gateway/       MAVLink <-> internal event translation
│   └── api/                     REST + WebSocket backend, telemetry store access
├── apps/
│   └── gcs-web/                 React GCS frontend (map + telemetry + commands)
├── simulation/
│   └── sitl/                    SITL launch scripts/config, docker-compose for sim
├── infra/
│   └── docker-compose.yml       local dev stack: sim + gateway + api + db + web
├── .github/workflows/           CI: lint, test, build, spin up SITL + smoke test
└── README.md
```

---

## 5. Core Data Models (Phase 1)

- **Vehicle** — id, name, type, connection status, last-seen timestamp
- **TelemetrySample** — vehicle_id, timestamp, lat, lon, alt, heading, roll,
  pitch, yaw, groundspeed, battery_voltage, battery_pct, gps_fix_type,
  armed (bool), flight_mode
- **Command** — id, vehicle_id, type (ARM/DISARM/TAKEOFF/LAND), issued_by,
  issued_at, status (PENDING/ACKED/FAILED), result
- **EventLogEntry** — append-only audit log of connection/arm/mode-change
  events, for later replay and debugging

---

## 6. Milestones & Timeline (indicative, 4–5 weeks)

| Week | Milestone |
|------|-----------|
| 1 | ADRs finalized; repo scaffolded; CI skeleton (lint + build) green; SITL boots locally and in CI |
| 2 | Telemetry gateway connects to SITL over MAVLink, parses core messages, publishes internal events; unit tests for message parsing |
| 3 | Backend API: telemetry persisted to time-series store; REST endpoints for vehicle state/history; WebSocket/SSE stream for live telemetry |
| 4 | GCS web UI: live map with vehicle marker, telemetry readout panel, connects to WS stream |
| 5 | Command path: arm/disarm + takeoff command from UI → API → gateway → SITL; end-to-end smoke test in CI; docs pass, exit-checklist review |

---

## 7. Dev Environment & CI

- **Local:** `docker-compose up` in `infra/` brings up SITL + telemetry-gateway
  + api + timescaledb + gcs-web in one command.
- **CI (GitHub Actions):**
  1. Lint + typecheck all services
  2. Unit tests per service
  3. Integration job: boot SITL in a container, run telemetry-gateway against
     it, assert telemetry flows end-to-end into the API (smoke test)
  4. Build all Docker images
- Branch protection: CI must pass before merge to `main`.

---

## 8. Testing Strategy

- **Unit:** MAVLink message parsing/mapping, API request validation, UI
  component tests for the telemetry panel and map marker updates.
- **Integration:** gateway ↔ SITL over real MAVLink/UDP in a container; API ↔
  DB round-trip tests.
- **End-to-end smoke test:** boot the full local stack, connect SITL, arm,
  command takeoff, assert altitude changes are visible via the API within a
  timeout — this becomes the CI gate for "the vertical slice still works."

---

## 9. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| SITL flakiness/boot time in CI | Cache SITL Docker image; generous but bounded startup timeout; retry once on CI-only infra failures |
| MAVLink protocol complexity | Use MAVSDK (higher-level SDK) instead of raw pymavlink for Phase 1; drop to raw protocol only if MAVSDK is missing something |
| Scope creep into fleet/mission features | Hold the line at "single simulated vehicle, minimal command set" for Phase 1 exit; anything else goes to the Phase 2 backlog |
| Time-series DB choice locking in early | Keep the storage access behind a small repository/interface in the API service so swapping TimescaleDB ↔ InfluxDB later doesn't ripple |

---

## 10. Deliverables

1. This plan, committed to `docs/PHASE_1_PLAN.md`
2. ADRs under `docs/adr/`
3. Working repo scaffold with CI green
4. Local docker-compose stack: simulator → telemetry → storage → API → UI
5. Demo: arm a simulated drone, command takeoff, watch telemetry update live
   on the map in the GCS UI

---

## 11. Open Questions (need decisions before/at Phase 1 kickoff)

- PX4 vs ArduPilot SITL — any existing team preference or hardware target
  further downstream that should decide this now?
- Single backend service (telemetry gateway + API combined) vs. two services
  as sketched above — is the extra service boundary worth it this early?
- Target for "who runs this" in Phase 1 — single developer machine only, or
  does a shared always-on dev environment matter yet?
