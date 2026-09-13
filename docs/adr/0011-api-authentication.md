# ADR-0011: API Authentication (Phase 5)

## Status
Accepted

## Context
Through Phases 1-4, every REST endpoint and WebSocket route on
`services/api` was reachable by anyone who could open a TCP connection to
it: `POST /vehicles/{id}/commands` (arm/disarm/takeoff/RTL) and
`POST /vehicles/{id}/missions` (upload/start a mission) had no
authentication at all. The only token check in the whole system
(`_check_internal_token` / `UAS_INTERNAL_TOKEN`) guards `POST
/internal/telemetry`, the gateway-to-API channel — it says nothing about
who's allowed to *command a vehicle* from the UI/API side. That is the
actual production blocker for this project, ahead of anything else on the
Phase 5+ roadmap (deploy infra, TLS, etc.): a reachable, unauthenticated
API can fly someone else's drone.

## Decision
Add a single **shared API key**, checked on every user-facing endpoint:

- REST: `X-API-Key: <key>` header, enforced by a `require_api_key`
  FastAPI dependency, applied to every route under `/vehicles` (list,
  get, telemetry, commands, missions, mission start) — not just the
  state-changing ones, since telemetry/mission history is also data an
  operator wouldn't want exposed to anyone who can reach the port.
- WebSocket: `?api_key=<key>` query parameter, checked by
  `require_ws_api_key()` before `ws_manager.connect()` (which calls
  `websocket.accept()`) on both `/ws/telemetry/{vehicle_id}` and
  `/ws/fleet`. Browsers cannot set custom headers on a WebSocket
  handshake, so the header approach used for REST doesn't carry over; a
  query parameter is the standard workaround, with the caveat noted
  below.
- **Deliberately excluded:** `GET /health` (must stay publicly probeable
  by orchestration/monitoring without a credential) and `POST
  /internal/telemetry` (already has its own, separate
  `UAS_INTERNAL_TOKEN` mechanism from Phase 1 — that's a different trust
  boundary: gateway-to-API, not operator-to-API).
- **Opt-in, off by default:** `Settings.api_key` (`API_KEY` env var)
  defaults to `""`. `require_api_key`/`require_ws_api_key` are no-ops
  whenever it's unset. This is a deliberate constraint, not an
  oversight: every phase of this project so far has been runnable with
  zero configuration (`./scripts/run_local.sh`, or the Windows
  PowerShell quick start), and Phase 5 must not silently break that for
  anyone who hasn't opted into auth. Setting `API_KEY` is what turns
  hardening on for an actual deployment.

## Consequences
- Set `API_KEY` (same value) on the API process and give it to every
  client — `apps/gcs-web` (via a `?api_key=` query param on the page
  URL, stored and replayed on every `fetch()`/WebSocket call — see
  `apps/gcs-web/app.js`), `scripts/smoke_test.py`, and anything else
  talking to the API — for it to take effect. Leaving it unset keeps
  Phase 1-4 behavior identical, verified by running the full existing
  test suite with `API_KEY` unset.
- **Known limitation — one shared secret, not per-user auth.** This is a
  single gate, not accounts, roles, or per-vehicle permissions: anyone
  holding the key can command every vehicle in the fleet. Adequate for
  this project's current scale (a handful of operators sharing one
  fleet); a **follow-up trigger, not built now**: per-user accounts,
  RBAC (e.g. "can view telemetry" vs "can arm/command"), or OAuth/JWT
  would be the answer once multiple distinct operators/organizations
  need to be told apart rather than merely kept out.
- **Known limitation — key-in-URL for WebSockets.** Query parameters can
  end up in server access logs and browser history. Acceptable for this
  project's threat model (a shared operational key, rotated like any
  other credential, not a per-user secret) but not appropriate for a
  more sensitive deployment. A **follow-up trigger, not built now**:
  move to a short-lived, per-connection token minted over the
  already-authenticated REST channel (fetched with `X-API-Key`, then
  handed to the WebSocket URL) if key exposure ever becomes a concern.
- **Known limitation — no transport encryption.** This ADR only adds
  *who's allowed in*; it says nothing about the key (or telemetry)
  being visible on the wire. Phase 1-4's `http://`/`ws://` everywhere is
  unchanged. TLS termination is ops/deploy-infra scope, not this
  repo's application code — left for whoever stands this up outside a
  local/trusted network.
