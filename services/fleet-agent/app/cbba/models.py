"""Data models for CBBA multi-drone inspection-point allocation
(docs/CBBA_PLAN.md).

Simulation only. An inspection point is a place a drone flies to and
looks at -- nothing else. These models reuse the existing Waypoint,
Geofence, SafetyConstraints and ValidationResult types from
app/core/models.py rather than defining parallel ones.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from app.core.models import Geofence, SafetyConstraints, ValidationResult, Waypoint

# Every CBBA result says what it actually is. Messages travel through an
# in-memory queue inside one Python process, in synchronous rounds --
# that is a simulation of distributed consensus, not the real thing
# (docs/CBBA_PLAN.md rule 4).
SIM_LABEL = "CBBA-sync-sim (in-process)"


class InspectionPoint(BaseModel):
    point_id: str = Field(min_length=1)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # The drone must carry this sensor to inspect the point; None = any drone.
    required_capability: str | None = None
    # Reward for inspecting the point, before time discounting.
    value: float = Field(default=100.0, gt=0)
    # Seconds spent hovering at the point (taking pictures).
    inspect_duration_s: float = Field(default=0.0, ge=0)
    # Latest allowed arrival, in seconds after the mission starts; None = no deadline.
    deadline_s: float | None = Field(default=None, gt=0)


class DroneHealth(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"


class DroneSpec(BaseModel):
    drone_id: str = Field(min_length=1)
    start: Waypoint
    battery_pct: float = Field(ge=0, le=100)
    capabilities: list[str] = Field(default_factory=list)
    health: DroneHealth = DroneHealth.OK


class CommsSpec(BaseModel):
    """Who can talk to whom. Either an explicit list of links, or every
    pair of drones whose start points are within range_m. Neither set
    means everyone hears everyone (fully connected)."""

    links: list[tuple[str, str]] | None = None
    range_m: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _one_mode(self) -> CommsSpec:
        if self.links is not None and self.range_m is not None:
            raise ValueError("give either links or range_m, not both")
        return self


class CbbaConfig(BaseModel):
    # Score of a point = value * discount_per_s ** (arrival time in s).
    # 0.999 per second halves a point's worth after about 11.5 minutes.
    discount_per_s: float = Field(default=0.999, gt=0, le=1)
    # L_t in the paper: the most points one drone may hold.
    max_bundle_size: int = Field(default=10, ge=1)
    # Hard stop so a bad input can never loop forever. Not reaching
    # convergence within this many rounds blocks dispatch.
    max_rounds: int = Field(default=500, ge=1)


class Scenario(BaseModel):
    drones: list[DroneSpec] = Field(min_length=1)
    points: list[InspectionPoint] = Field(min_length=1)
    geofence: Geofence
    constraints: SafetyConstraints = Field(default_factory=SafetyConstraints)
    comms: CommsSpec = Field(default_factory=CommsSpec)
    config: CbbaConfig = Field(default_factory=CbbaConfig)
    # Altitude of the lowest drone; each next drone (by id) flies one
    # altitude band higher (app/safety/validator.py::assign_altitude_bands).
    base_altitude_m: float = Field(default=20.0, gt=0)

    @model_validator(mode="after")
    def _unique_ids(self) -> Scenario:
        drone_ids = [d.drone_id for d in self.drones]
        point_ids = [p.point_id for p in self.points]
        if len(set(drone_ids)) != len(drone_ids):
            raise ValueError("drone ids must be unique")
        if len(set(point_ids)) != len(point_ids):
            raise ValueError("point ids must be unique")
        for a, b in self.comms.links or []:
            if a not in drone_ids or b not in drone_ids:
                raise ValueError(f"comms link {a}-{b} names an unknown drone")
        return self


class ExcludedItem(BaseModel):
    item_id: str
    reason: str


class DronePlan(BaseModel):
    drone_id: str
    altitude_m: float
    # Visiting order (the CBBA "path"), and the order points were won
    # (the CBBA "bundle").
    path: list[str]
    bundle: list[str]
    arrival_s: list[float]
    score: float
    route_m: float
    route: list[Waypoint]  # start -> each point -> start, at altitude_m


class CbbaResult(BaseModel):
    label: str = SIM_LABEL
    method: str  # "CBBA" or "SGA" (the centralised reference)
    converged: bool
    # Rounds run in total, and the last round in which any drone's view
    # changed. rounds - last_change_round = rounds spent confirming that
    # nothing changes any more (D rounds, D = comms-graph diameter).
    rounds: int
    last_change_round: int
    messages_sent: int
    # True when every drone that can hear each other (directly or through
    # others) ended with identical winner lists.
    agreement: bool
    # Groups of drones that can hear each other (directly or by relay) at
    # the end of the run. More than one group = the radio network is split.
    comms_groups: list[list[str]]
    plans: list[DronePlan]
    winners: dict[str, str | None]
    total_score: float
    unassigned: list[ExcludedItem]
    excluded_drones: list[ExcludedItem]
    excluded_points: list[ExcludedItem]
    safety: ValidationResult
    # The only flag later phases may act on: converged AND safety passed.
    dispatch_allowed: bool
