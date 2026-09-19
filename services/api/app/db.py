from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

_is_sqlite = settings.database_url.startswith("sqlite")

connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

if _is_sqlite:
    # Phase 3: multiple gateway processes (one per fleet vehicle, see
    # docs/adr/0009-multi-vehicle-fleet.md) now POST /internal/telemetry
    # concurrently. SQLite's default rollback-journal mode serializes
    # writers and can raise "database is locked" under contention. WAL
    # mode lets readers proceed without blocking on a writer, and
    # busy_timeout makes a writer that does have to wait retry for a
    # while instead of failing immediately. This is a tested stopgap for
    # a small fleet, not a replacement for the eventual Postgres/
    # TimescaleDB migration ADR-0004 already anticipates for real scale.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def _sqlite_literal_default(column) -> str:
    """SQL literal for column's Python-level default, for the ALTER
    TABLE ... ADD COLUMN ... DEFAULT clause below. Only handles the
    simple scalar defaults this project's models actually use (a bool,
    number, or string) -- anything else (a callable default, e.g. the
    utcnow()/uuid4() ones in models.py) raises rather than silently
    guessing wrong, since a column with one of those is never something
    _backfill_missing_sqlite_columns() below needs to handle in
    practice: they're all either primary keys (present since table
    creation, never "added later") or timestamps where NULL for old
    rows is the honest answer, not a fabricated backfilled time."""
    if column.default is None:
        return "NULL"
    default = column.default.arg
    if callable(default):
        raise NotImplementedError(
            f"column {column.name!r} has a callable default -- "
            "_backfill_missing_sqlite_columns() only handles scalar defaults"
        )
    if isinstance(default, bool):
        return "1" if default else "0"
    if isinstance(default, (int, float)):
        return str(default)
    return "'" + str(default).replace("'", "''") + "'"


def _backfill_missing_sqlite_columns(bind=None) -> None:
    """create_all() (below) only creates TABLES that don't exist yet --
    it never alters a table that's already there. That's invisible on a
    brand new uas.db, but this project has no formal migration system
    (docs/adr/0004-telemetry-storage.md defers that to the eventual
    Postgres move), so a long-lived local dev uas.db from an earlier
    phase can be missing a column a later phase's model added (e.g.
    Vehicle.emergency_state in Phase 6a) -- bare create_all() would
    never notice, and every request touching that table then fails with
    a confusing "no such column" 500 instead of anything explaining why.
    This adds exactly the missing columns (nothing else -- no dropped or
    renamed column handling, no data transformation) so pulling a new
    phase's code and restarting the API against an existing uas.db just
    works, the same as it would against a fresh one."""
    bind = bind or engine
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    with bind.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # brand new table -- create_all() already handled it
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_cols:
                    continue
                default_sql = _sqlite_literal_default(column)
                conn.execute(
                    text(
                        f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN "{column.name}" {column.type} DEFAULT {default_sql}'
                    )
                )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    if _is_sqlite:
        _backfill_missing_sqlite_columns()


def get_session() -> Session:
    return SessionLocal()
