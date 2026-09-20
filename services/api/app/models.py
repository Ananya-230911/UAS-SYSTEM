import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Vehicle(Base):
    """A single UAS. Phase 1 runs exactly one (the simulator), but nothing
    here assumes that -- vehicle_id is a free-form string key throughout."""

    __tablename__ = "vehicles"

    vehicle_id: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    armed: Mapped[bool] = mapped_column(Boolean, default=False)
    flight_mode: Mapped[str] = mapped_column(String, default="UNKNOWN")
    current_waypoint_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_mission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Phase 6a (docs/adr/0012-geofencing-failsafe.md): NORMAL or
    # GEOFENCE_BREACH. Set by the failsafe manager in ingest_telemetry()
    # when armed telemetry lands outside the vehicle's geofence (if any);
    # reset to NORMAL when the vehicle disarms. Deliberately just these
    # two states for now -- see the ADR for the fuller state machine this
    # could grow into (e.g. a distinct "RTL_COMMANDED" state) if a second
    # failsafe trigger is ever added.
    emergency_state: Mapped[str] = mapped_column(String, default="NORMAL")
    # Phase 6b (docs/adr/0013-risk-monitoring-and-remote-id.md): a
    # heuristic (not learned/ML) risk assessment recomputed on every
    # telemetry ingest -- LOW/MEDIUM/HIGH plus the specific flags that
    # produced it (e.g. "RAPID_BATTERY_DRAIN"). Purely advisory: unlike
    # emergency_state above, nothing here ever auto-commands the
    # vehicle -- see app/risk_monitor.py and the ADR for why that's a
    # deliberate line this feature doesn't cross. risk_flags stays
    # nullable rather than defaulting to an empty list so the SQLite
    # column-backfill safety net (app/db.py) can give it a plain literal
    # default; every row gets a real (possibly empty) list the moment
    # its next telemetry sample is ingested.
    risk_level: Mapped[str] = mapped_column(String, default="LOW")
    risk_flags: Mapped[list | None] = mapped_column(JSON, nullable=True)


class TelemetrySample(Base):
    """One telemetry snapshot. Indexed for the (vehicle, time-range) query
    pattern -- the same pattern a TimescaleDB hypertable is built for, see
    docs/adr/0004-telemetry-storage.md."""

    __tablename__ = "telemetry_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[str] = mapped_column(
        String, ForeignKey("vehicles.vehicle_id"), index=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    alt_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    relative_alt_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    groundspeed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    roll: Mapped[float | None] = mapped_column(Float, nullable=True)
    pitch: Mapped[float | None] = mapped_column(Float, nullable=True)
    yaw: Mapped[float | None] = mapped_column(Float, nullable=True)
    battery_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    battery_voltage_mv: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_fix_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    satellites_visible: Mapped[int | None] = mapped_column(Integer, nullable=True)
    armed: Mapped[bool] = mapped_column(Boolean, default=False)
    flight_mode: Mapped[str] = mapped_column(String, default="UNKNOWN")
    current_waypoint_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Mission(Base):
    """An uploaded set of waypoints for a vehicle to fly. See
    docs/adr/0008-mission-protocol.md for the upload protocol and the
    documented simplification that this phase does not auto-detect
    mission completion (no COMPLETED status)."""

    __tablename__ = "missions"

    mission_id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    vehicle_id: Mapped[str] = mapped_column(
        String, ForeignKey("vehicles.vehicle_id"), index=True
    )
    waypoints: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="PENDING")  # PENDING/UPLOADED/ACTIVE/FAILED
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Command(Base):
    __tablename__ = "commands"

    command_id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    vehicle_id: Mapped[str] = mapped_column(
        String, ForeignKey("vehicles.vehicle_id"), index=True
    )
    command_type: Mapped[str] = mapped_column(String)
    altitude_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, default="PENDING")  # PENDING/ACKED/FAILED
    mav_result: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Geofence(Base):
    """One polygon boundary per vehicle (docs/adr/0012-geofencing-failsafe.md).
    Keyed by vehicle_id itself (not a separate id) because a vehicle has at
    most one active fence -- POST replaces it outright, matching how
    Mission upload replaces "the current plan" rather than layering
    fences. points is a JSON list of {"lat":, "lon":}, at least 3 of them
    (see schemas.GeofenceCreate)."""

    __tablename__ = "geofences"

    vehicle_id: Mapped[str] = mapped_column(
        String, ForeignKey("vehicles.vehicle_id"), primary_key=True
    )
    points: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EventLogEntry(Base):
    """Append-only audit log -- connection changes, commands, arm/mode
    transitions. Kept separate from TelemetrySample so high-frequency
    telemetry writes don't bloat the event table."""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[str] = mapped_column(String, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    event_type: Mapped[str] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
