"""Coverage for the Phase 3 SQLite concurrency hardening (WAL mode +
busy_timeout) -- see docs/adr/0009-multi-vehicle-fleet.md."""
import sqlite3

from sqlalchemy import create_engine, text

from app.db import _backfill_missing_sqlite_columns, engine


def test_sqlite_wal_mode_is_enabled():
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode.lower() == "wal"


def test_sqlite_busy_timeout_is_set():
    with engine.connect() as conn:
        timeout_ms = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert timeout_ms == 5000


def test_backfill_adds_a_column_a_later_phase_added_with_its_default(tmp_path):
    # Reproduces exactly the bug this backfill exists to prevent: a local
    # dev uas.db created before Phase 6a added Vehicle.emergency_state.
    # Without the backfill, every query against `vehicles` fails with
    # "no such column: vehicles.emergency_state" (a 500 from the API)
    # the instant a repo checkout is updated past that phase and pointed
    # at an old database file, rather than being invisible the way a
    # brand-new uas.db always was.
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE vehicles ("
        "vehicle_id VARCHAR PRIMARY KEY, first_seen DATETIME, last_seen DATETIME, "
        "armed BOOLEAN, flight_mode VARCHAR, current_waypoint_seq INTEGER, "
        "active_mission_id VARCHAR)"
    )
    conn.execute(
        "INSERT INTO vehicles (vehicle_id, armed, flight_mode) VALUES ('old-vehicle', 0, 'STANDBY')"
    )
    conn.commit()
    conn.close()

    test_engine = create_engine(f"sqlite:///{db_path}")
    _backfill_missing_sqlite_columns(test_engine)

    with test_engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(vehicles)"))}
        assert "emergency_state" in cols
        # Not just present -- backfilled with the model's actual default
        # for the row that existed before the column did, not NULL.
        value = conn.execute(
            text("SELECT emergency_state FROM vehicles WHERE vehicle_id = 'old-vehicle'")
        ).scalar()
        assert value == "NORMAL"


def test_backfill_is_a_no_op_on_an_up_to_date_database(tmp_path):
    # Regression guard: running it again (e.g. every init_db() call on
    # every API startup) must not error on a database that already has
    # every column -- ALTER TABLE ADD COLUMN on an existing column would.
    from app.models import Base

    db_path = tmp_path / "current.db"
    test_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=test_engine)
    _backfill_missing_sqlite_columns(test_engine)  # must not raise
