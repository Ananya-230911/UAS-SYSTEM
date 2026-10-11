"""Shared data models for the multi-agent fleet module
(docs/adr/0016-fleet-agent-phase-2-models-and-safety.md).

Phase 2 of docs/IMPLEMENTATION_PLAN.md: one typed "language" every
later phase (planning, agents, comms, the GCS) builds on, independent
of any AI/LLM. Nothing here talks to a gateway, a vehicle, or an LLM --
it's pure Pydantic schema, directly unit-testable.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Waypoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # Loose sanity bound at the schema layer only (catches obvious
    # garbage, e.g. a negative or absurd altitude) -- the real,
    # mission-specific min/max enforcement is SafetyConstraints' job in
    # safety/validator.py, since what counts as a valid altitude
    # depends on the mission, not a fixed schema constant.
    alt_m: float = Field(ge=-5, le=1000)


class GeofencePoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class Geofence(BaseModel):
    # A polygon needs at least 3 points to enclose any area -- same
    # rule services/api/app/schemas.py's GeofenceCreate already uses.
    points: list[GeofencePoint] = Field(min_length=3)


class SearchArea(BaseModel):
    """The area an operator wants covered/searched -- a polygon, same
    shape as Geofence but a distinct concept (a search area is where to
    look; a geofence is where you're allowed to be -- a search area
    should normally fall inside the geofence, but the schema doesn't
    assume that; the safety validator checks it)."""

    points: list[GeofencePoint] = Field(min_length=3)


class SafetyConstraints(BaseModel):
    """Mission-specific limits the safety validator enforces. Defaults
    are reasonable demonstration values, not validated against real
    flight data -- the same caveat this project's other heuristic
    thresholds (e.g. services/api/app/risk_monitor.py) already carry.
    cruise_speed_ms and battery_drain_pct_per_min default to
    simulation/sitl/sim.py's own constants (WAYPOINT_SPEED_MS=8.0, and
    its 0.01%/second armed drain rate = 0.6%/minute) so an endurance
    estimate actually matches what the simulator will do, not an
    arbitrary guess.
    """

    min_altitude_m: float = 5.0
    max_altitude_m: float = 120.0
    # Vertical gap between adjacent vehicles' assigned altitude bands.
    altitude_band_spacing_m: float = 5.0
    # Minimum horizontal gap required between two vehicles' routes when
    # they are NOT already vertically deconflicted by altitude_band_spacing_m.
    min_horizontal_separation_m: float = 10.0
    # Battery must stay at or above this percentage after flying the
    # route plus returning home.
    battery_reserve_pct: float = 20.0
    cruise_speed_ms: float = 8.0
    battery_drain_pct_per_min: float = 0.6


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REASSIGNED = "REASSIGNED"


class Task(BaseModel):
    """One unit of work -- a coverage cell's route, in Phase 3's
    lawnmower-search terms (planning/coverage.py, not built yet)."""

    task_id: str
    cell_id: str
    waypoints: list[Waypoint] = Field(min_length=1)
    status: TaskStatus = TaskStatus.PENDING


class TaskAssignment(BaseModel):
    """Which vehicle a task was auctioned to (planning/allocation.py,
    not built yet), and at what bid cost -- kept here, not folded into
    Task, so re-auctioning (Phase 4: battery/link loss) only replaces
    the assignment, never has to touch or duplicate the task/route
    itself."""

    task_id: str
    vehicle_id: str
    bid_cost: float


class MissionRequest(BaseModel):
    """What an operator (or, later, the Phase 3 goal parser) is asking
    for -- the input to planning, not yet a concrete plan."""

    goal_text: str
    vehicle_ids: list[str] = Field(min_length=1)
    search_area: SearchArea | None = None
    altitude_m: float = 20.0
    requested_at: datetime = Field(default_factory=_utcnow)


class MissionPlan(BaseModel):
    """A structured, concrete plan: every vehicle's tasks and route
    assignments, ready to be validated and (once approved) executed."""

    plan_id: str
    mission_request: MissionRequest
    tasks: list[Task]
    assignments: list[TaskAssignment]

    def content_hash(self) -> str:
        """A deterministic hash of everything about this plan's
        CONTENT -- deliberately excluding plan_id itself (an identity,
        not content). This is the core of "execution is refused if the
        plan changed after approval" (docs/IMPLEMENTATION_PLAN.md
        Phase 2): ApprovalRecord stores this hash at approval time, and
        safety/validator.py's plan_matches_approval() recomputes it
        before execution -- any change to tasks/assignments/mission_request
        changes the hash and the comparison fails, exactly like a
        tamper-evident checksum."""
        payload = self.model_dump(mode="json", exclude={"plan_id"})
        canonical = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


class ValidationIssue(BaseModel):
    code: str
    message: str
    vehicle_id: str | None = None
    task_id: str | None = None


class ValidationResult(BaseModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class ApprovalRecord(BaseModel):
    plan_id: str
    plan_hash: str
    approved_by: str
    approved_at: datetime = Field(default_factory=_utcnow)


class ExecutionStatus(str, Enum):
    """Mission lifecycle (docs/IMPLEMENTATION_PLAN.md section 3's
    Phase 6 state list). Not wired to anything yet -- this is just the
    typed vocabulary later phases' mission/manager.py will use."""

    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class TelemetrySnapshot(BaseModel):
    """The minimal shape of a vehicle's telemetry this module needs --
    deliberately a subset of services/api/app/schemas.py's
    TelemetryOut, not a reference to it (services stay independent on
    purpose, see docs/adr/0006-internal-messaging.md)."""

    vehicle_id: str
    lat: float | None = None
    lon: float | None = None
    relative_alt_m: float | None = None
    battery_pct: float | None = None
    armed: bool = False
    flight_mode: str = "UNKNOWN"
    timestamp: datetime


class AgentMessage(BaseModel):
    """A mesh-bus message between per-vehicle agents (Phase 4's
    comms/bus.py, not built yet) -- modeled now so later phases have a
    schema to build directly on rather than inventing one ad hoc."""

    message_id: str
    sender_id: str
    recipient_id: str | None = None  # None = broadcast
    message_type: str
    payload: dict
    sent_at: datetime = Field(default_factory=_utcnow)
    sequence: int = 0


class Detection(BaseModel):
    """A single object detection reported by a vehicle (Phase 5, not
    built yet) -- deliberately display/logging data only, see
    docs/IMPLEMENTATION_PLAN.md section 3's explicit out-of-scope
    boundary (no targeting/engagement logic anywhere in this system)."""

    class_name: str
    confidence: float = Field(ge=0, le=1)
    lat: float | None = None
    lon: float | None = None
    vehicle_id: str
    detected_at: datetime = Field(default_factory=_utcnow)


class Event(BaseModel):
    """A loggable, explainable thing that happened -- an agent
    decision, a re-auction, a failsafe trigger -- for the GCS event
    timeline (Phase 6, not built yet)."""

    event_type: str
    vehicle_id: str | None = None
    detail: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)
