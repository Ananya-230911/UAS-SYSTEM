"""Coverage for the Phase 3 SQLite concurrency hardening (WAL mode +
busy_timeout) -- see docs/adr/0009-multi-vehicle-fleet.md."""
from sqlalchemy import text

from app.db import engine


def test_sqlite_wal_mode_is_enabled():
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode.lower() == "wal"


def test_sqlite_busy_timeout_is_set():
    with engine.connect() as conn:
        timeout_ms = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert timeout_ms == 5000
