# ADR-0009: Multi-Vehicle Fleet

## Status
Accepted

## Context
Phase 3 needs to support more than one vehicle at a time: a fleet view in
the UI, and the backend actually tracking several vehicles concurrently.

## Decision
**Scale by running more (simulator, telemetry-gateway) process pairs, not
by rewriting the gateway to be multi-vehicle-aware.** Every piece of
per-vehicle identity (`VEHICLE_ID`, `GATEWAY_MAVLINK_PORT`,
`GATEWAY_PORT`) was already an environment variable in Phase 1/2 — a
second vehicle is a second `sim.py` + a second gateway process on
different ports, both pointed at the same `services/api`. No gateway or
simulator code changed for this phase.

The API and its WebSocket fan-out were already vehicle-agnostic
(`vehicle_id` has been a free-form key on every table and every route
since Phase 1 — see `models.py`'s comment on `Vehicle`). The one gap:
there was no way for the browser to receive *every* vehicle's telemetry
over a single connection without knowing all vehicle IDs upfront. Added
`WS /ws/fleet`, which receives every vehicle's telemetry (the existing
per-vehicle `WS /ws/telemetry/{id}` is unchanged and still works).

**SQLite concurrency:** multiple gateway processes now `POST
/internal/telemetry` concurrently. Enabled WAL mode + a busy timeout
(`services/api/app/db.py`) rather than migrating to Postgres/TimescaleDB
now — tested empirically with a 3-vehicle fleet in the smoke test with no
"database is locked" errors. This is deliberately not the Postgres
migration ADR-0004 already anticipates; that remains the answer once
fleet size (not just a handful of vehicles) makes SQLite's single-writer
model the actual bottleneck.

**No pub/sub layer added.** ADR-0006 named the trigger for introducing
one as "either fleet size or API horizontal scaling." A few-to-dozens of
vehicles hitting one API process is well within what the existing
in-process `asyncio` WebSocket broadcast handles; nothing here needs a
second API replica yet.

## Consequences
- Adding vehicle N+1 is an ops/config change (another process pair,
  another docker-compose service), not a code change.
- `apps/gcs-web` gained a fleet list + map showing all known vehicles,
  with commands/mission actions scoped to whichever vehicle is selected.
- **Known limitation:** the UI discovers vehicles via `GET /vehicles`
  (existing endpoint) plus whatever arrives on `/ws/fleet` after that — a
  vehicle that has never sent telemetry won't appear. Fine for a fleet
  where every vehicle is expected to be live; not a vehicle *registry* in
  the fullest sense (Phase 5 territory if that's ever needed).
- **Follow-up trigger, not built now:** once a fleet is large enough that
  SQLite's WAL mode stops being enough (sustained write contention,
  measured, not assumed), do the Postgres/TimescaleDB migration
  ADR-0004 already scoped.
