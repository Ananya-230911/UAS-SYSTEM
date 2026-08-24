# ADR-0004: Telemetry Storage

## Status
Accepted (with a Phase 1 substitution)

## Context
Telemetry arrives at a few Hz per vehicle and needs to be queryable by time
range for history/replay later.

## Decision
Target **TimescaleDB** (Postgres + time-series extension) for real
deployments — SQL, range queries, and continuous aggregates for free, and one
less moving part than a separate time-series database.

**Phase 1 substitution:** `services/api` uses **SQLite** via SQLAlchemy for
local development and CI, behind a plain repository-style data-access layer
(`app/models.py` + query functions in `app/main.py`). This was chosen over
standing up TimescaleDB/Postgres in this environment (no working Docker
daemon in the sandbox this was built in) purely to keep Phase 1's local
loop to "run three Python processes," not "stand up a database server."

## Consequences
- The `TelemetrySample` table is indexed on `(vehicle_id, timestamp)`, which
  is the access pattern both SQLite and TimescaleDB support well — no
  redesign needed when migrating.
- Swapping SQLite → TimescaleDB is changing `app/db.py`'s connection URL and
  swapping the SQLAlchemy dialect; no query code should need to change
  because none of it uses SQLite-specific features.
- SQLite's single-writer behavior is a real limitation the moment more than
  one gateway writes concurrently (multi-vehicle fleets). This is acceptable
  for Phase 1 (one simulated vehicle) and is the concrete trigger for doing
  the Postgres/TimescaleDB migration in Phase 2, not something to defer
  indefinitely.
