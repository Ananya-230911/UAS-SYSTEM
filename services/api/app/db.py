from sqlalchemy import create_engine, event
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


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_session() -> Session:
    return SessionLocal()
