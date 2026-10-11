"""One goal -> a plan for every drone (docs/IMPLEMENTATION_PLAN.md Phase 3).

plan_mission() is deterministic code end to end: coverage cells, a
lawnmower route per cell, a greedy auction to split cells between
drones, per-drone altitude bands, then the Phase 2 safety validator on
the finished plan. The result carries a plain-English explanation of
who got what and why. Nothing here sends a command.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.core.models import (
    Geofence,
    MissionPlan,
    MissionRequest,
    SafetyConstraints,
    SearchArea,
    Task,
    TaskAssignment,
    ValidationIssue,
    ValidationResult,
    VehicleRole,
    Waypoint,
)
from app.planning.allocation import VehicleState, assign_roles, auction_cells
from app.planning.coverage import camera_footprint_spacing_m, lawnmower_path, path_length_m, split_into_cells
from app.safety.geofence import point_in_polygon
from app.safety.validator import assign_altitude_bands, validate_mission_plan


@dataclass
class PlanningResult:
    plan: MissionPlan | None
    validation: ValidationResult
    explanations: list[str] = field(default_factory=list)


def _compass(lat: float, lon: float, area: SearchArea) -> str:
    lats = [p.lat for p in area.points]
    lons = [p.lon for p in area.points]
    mid_lat, mid_lon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    tol_lat, tol_lon = (max(lats) - min(lats)) / 6, (max(lons) - min(lons)) / 6
    ns = "north" if lat > mid_lat + tol_lat else "south" if lat < mid_lat - tol_lat else ""
    ew = "east" if lon > mid_lon + tol_lon else "west" if lon < mid_lon - tol_lon else ""
    return f"{ns}-{ew}" if ns and ew else (ns or ew or "centre")


def plan_mission(
    request: MissionRequest,
    geofence: Geofence,
    constraints: SafetyConstraints,
    vehicles: dict[str, VehicleState],
    cells_per_vehicle: int = 2,
    camera_fov_deg: float = 60.0,
) -> PlanningResult:
    area = request.search_area or SearchArea(points=geofence.points)

    if not (constraints.min_altitude_m <= request.altitude_m <= constraints.max_altitude_m):
        return PlanningResult(
            plan=None,
            validation=ValidationResult(
                valid=False,
                issues=[
                    ValidationIssue(
                        code="ALTITUDE_OUT_OF_RANGE",
                        message=(
                            f"requested altitude {request.altitude_m}m is outside "
                            f"[{constraints.min_altitude_m}, {constraints.max_altitude_m}]m"
                        ),
                    )
                ],
            ),
        )

    missing = [vid for vid in request.vehicle_ids if vid not in vehicles]
    if missing:
        return PlanningResult(
            plan=None,
            validation=ValidationResult(
                valid=False,
                issues=[
                    ValidationIssue(code="UNKNOWN_VEHICLE", message=f"no position/battery known for {vid}", vehicle_id=vid)
                    for vid in missing
                ],
            ),
        )

    # A drone that isn't inside the operating area can't take part. Left
    # out here with a stated reason -- the validator independently
    # re-checks every drone the plan names, so this isn't the only guard.
    fence_polygon = [p.model_dump() for p in geofence.points]
    outside = {
        vid: "its launch point is outside the geofence"
        for vid in request.vehicle_ids
        if not point_in_polygon(vehicles[vid].home.lat, vehicles[vid].home.lon, fence_polygon)
    }
    fleet = [vehicles[vid] for vid in request.vehicle_ids if vid not in outside]
    if not fleet:
        return PlanningResult(
            plan=None,
            validation=ValidationResult(
                valid=False,
                issues=[ValidationIssue(code="NO_ELIGIBLE_VEHICLES", message="every requested drone is outside the geofence")],
            ),
            explanations=[f"{vid} was left out: {why}." for vid, why in sorted(outside.items())],
        )
    spacing_m = camera_footprint_spacing_m(request.altitude_m, fov_deg=camera_fov_deg)
    cells = split_into_cells(area, len(fleet) * cells_per_vehicle)
    if not cells:
        return PlanningResult(
            plan=None,
            validation=ValidationResult(
                valid=False,
                issues=[ValidationIssue(code="EMPTY_SEARCH_AREA", message="the search area produced no cells to cover")],
            ),
        )

    # Sweep length per cell (at the base altitude -- the lowest band, so
    # the spacing is conservative for drones flying higher).
    sweeps = {c.cell_id: lawnmower_path(c, spacing_m, request.altitude_m) for c in cells}
    auction = auction_cells(
        cells,
        fleet,
        min_battery_pct=constraints.battery_reserve_pct,
        cell_work_m={cid: path_length_m(wps) for cid, wps in sweeps.items()},
    )
    excluded = {**outside, **auction.excluded}
    # Only drones that actually won work are part of the plan -- a drone
    # with no cells is not a participant (and must not be named
    # coordinator).
    participant_ids = sorted({a.vehicle_id for a in auction.awards})
    idle_ids = sorted(v.vehicle_id for v in fleet if v.vehicle_id not in excluded and v.vehicle_id not in participant_ids)
    roles = assign_roles(participant_ids)
    bands = assign_altitude_bands(participant_ids, constraints, base_altitude_m=request.altitude_m)

    tasks: list[Task] = []
    assignments: list[TaskAssignment] = []
    for vid in participant_ids:
        for award in auction.cells_for(vid):
            task_id = f"{vid}:{award.cell.cell_id}"
            waypoints = [wp.model_copy(update={"alt_m": bands[vid]}) for wp in sweeps[award.cell.cell_id]]
            tasks.append(
                Task(task_id=task_id, cell_id=award.cell.cell_id, waypoints=waypoints, cell_polygon=award.cell.polygon)
            )
            assignments.append(TaskAssignment(task_id=task_id, vehicle_id=vid, bid_cost=award.bid_cost))

    left_out = [f"{vid} was left out: {why}." for vid, why in sorted(excluded.items())]
    if not tasks:
        reasons = "; ".join(f"{vid}: {why}" for vid, why in sorted(excluded.items()))
        return PlanningResult(
            plan=None,
            validation=ValidationResult(
                valid=False,
                issues=[ValidationIssue(code="NO_ELIGIBLE_VEHICLES", message=f"no drone can take the work ({reasons})")],
            ),
            explanations=left_out,
        )

    plan = MissionPlan(
        plan_id=str(uuid.uuid4()),
        mission_request=request.model_copy(update={"vehicle_ids": participant_ids}),
        tasks=tasks,
        assignments=assignments,
        roles=roles,
    )
    homes = {vid: vehicles[vid].home for vid in participant_ids}
    batteries = {vid: vehicles[vid].battery_pct for vid in participant_ids}
    validation = validate_mission_plan(plan, geofence, constraints, homes, batteries)

    explanations = [
        f"Sweep lines are {spacing_m:.1f} m apart (camera footprint at {request.altitude_m:.0f} m, "
        f"{camera_fov_deg:.0f} degree field of view, 20% overlap); the area is split into {len(cells)} cells."
    ]
    for vid in participant_ids:
        won = auction.cells_for(vid)
        state = vehicles[vid]
        route_m = path_length_m([state.home] + [wp for a in won for wp in sweeps[a.cell.cell_id]])
        where = sorted({_compass(*a.cell.centroid, area) for a in won})
        role = "coordinator, runs the auction and also searches" if roles[vid] == VehicleRole.COORDINATOR else "scout"
        explanations.append(
            f"{vid} ({role}) gets {len(won)} cell(s) in the {', '.join(where)} of the area "
            f"[{', '.join(a.cell.cell_id for a in won)}]: its first cell is {won[0].leg_m:.0f} m from where it is, "
            f"it has {state.battery_pct:.0f}% battery, and it flies at {bands[vid]:.0f} m. "
            f"Route about {route_m / 1000:.2f} km."
        )
    for vid in idle_ids:
        explanations.append(f"{vid} got no cells -- the other drones' bids were always lower.")
    explanations += left_out

    return PlanningResult(plan=plan, validation=validation, explanations=explanations)
