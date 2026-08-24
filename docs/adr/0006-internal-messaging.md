# ADR-0006: Internal Messaging

## Status
Accepted

## Context
Telemetry needs to flow from the point it's received (gateway) to where it's
persisted and streamed to clients (API), and commands need to flow the other
way.

## Decision
No message broker in Phase 1. Two mechanisms, both already visible at the
process/service boundary:

1. **Gateway → API:** the gateway POSTs normalized telemetry snapshots to the
   API's `POST /internal/telemetry` endpoint (a shared-secret header,
   `X-Internal-Token`, stands in for real service auth in Phase 1).
2. **API → Gateway:** the API POSTs command requests to the gateway's
   `POST /command` endpoint and blocks for the synchronous MAVLink
   COMMAND_ACK result.
3. **API → UI (fan-out to browser clients):** in-process `asyncio`
   WebSocket broadcast (`app/ws_manager.py`) — telemetry received by the API
   is pushed directly to connected WebSocket clients, no intermediary queue.

## Consequences
- No Redis/NATS/Kafka to operate in Phase 1.
- This does not scale to multiple gateways/API replicas needing to share
  fan-out state — the WebSocket broadcast is per-process. That's fine for
  one API instance and one vehicle; it's the concrete trigger for
  introducing a real pub/sub layer (Redis or NATS) once either fleet size or
  API horizontal scaling makes it necessary, not something to build ahead of
  need.
