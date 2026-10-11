"""The strict safety checker (docs/adr/0016-fleet-agent-phase-2-models-and-safety.md).

Phase 2 of docs/IMPLEMENTATION_PLAN.md: every one of these checks is
deterministic Python, independent of any AI/LLM -- the AI (Phase 3,
not built yet) will only ever PROPOSE a MissionPlan; this module is
what actually decides whether it's safe. Nothing here talks to a
gateway or a vehicle yet -- that wiring ("every command to the gateway
passes through the validator") is Phase 5's job, once there's an
executor to wire it into.
"""
from __future__ import annotations

from app.core.models import (
    ApprovalRecord,
    Geofence,
    MissionPlan,
    SafetyConstraints,
    ValidationIssue,
    ValidationResult,
    Waypoint,
)
from app.safety.geofence import (
    horizontal_distance_m,
    point_in_polygon,
    segment_crosses_fence_boundary,
)


def check_altitude(route: list[Waypoint], constraints: SafetyConstraints, vehicle_id: str) -> list[ValidationIssue]:
    issues = []
    for i, wp in enumerate(route):
        if not (constraints.min_altitude_m <= wp.alt_m <= constraints.max_altitude_m):
            issues.append(
                ValidationIssue(
                    code="ALTITUDE_OUT_OF_RANGE",
                    message=(
                        f"{vehicle_id} waypoint {i} altitude {wp.alt_m}m is outside "
                        f"[{constraints.min_altitude_m}, {constraints.max_altitude_m}]m"
                    ),
                    vehicle_id=vehicle_id,
                )
            )
    return issues


def check_route_against_fence(route: list[Waypoint], geofence: Geofence, vehicle_id: str) -> list[ValidationIssue]:
    polygon = [p.model_dump() for p in geofence.points]
    issues = []
    for i, wp in enumerate(route):
        if not point_in_polygon(wp.lat, wp.lon, polygon):
            issues.append(
                ValidationIssue(
                    code="WAYPOINT_OUTSIDE_FENCE",
                    message=f"{vehicle_id} waypoint {i} is outside the geofence",
                    vehicle_id=vehicle_id,
                )
            )
    for i in range(len(route) - 1):
        p1 = {"lat": route[i].lat, "lon": route[i].lon}
        p2 = {"lat": route[i + 1].lat, "lon": route[i + 1].lon}
        if segment_crosses_fence_boundary(p1, p2, polygon):
            issues.append(
                ValidationIssue(
                    code="SEGMENT_CROSSES_FENCE",
                    message=f"{vehicle_id} route segment {i}->{i + 1} crosses outside the geofence boundary",
                    vehicle_id=vehicle_id,
                )
            )
    return issues


def check_launch_point(home: Waypoint, geofence: Geofence, vehicle_id: str) -> list[ValidationIssue]:
    polygon = [p.model_dump() for p in geofence.points]
    if not point_in_polygon(home.lat, home.lon, polygon):
        return [
            ValidationIssue(
                code="LAUNCH_POINT_OUTSIDE_FENCE",
                message=f"{vehicle_id}'s launch/home point is outside the geofence",
                vehicle_id=vehicle_id,
            )
        ]
    return []


def check_endurance(
    route: list[Waypoint],
    home: Waypoint,
    constraints: SafetyConstraints,
    battery_pct: float,
    vehicle_id: str,
) -> list[ValidationIssue]:
    """Estimated flight time for home -> route -> home, at
    constraints.cruise_speed_ms, must not drain the vehicle below
    constraints.battery_reserve_pct by the time it's back home."""
    points = [home] + route + [home]
    total_m = sum(
        horizontal_distance_m(
            {"lat": points[i].lat, "lon": points[i].lon},
            {"lat": points[i + 1].lat, "lon": points[i + 1].lon},
        )
        for i in range(len(points) - 1)
    )
    flight_time_min = (total_m / constraints.cruise_speed_ms) / 60.0
    battery_needed_pct = flight_time_min * constraints.battery_drain_pct_per_min
    battery_after = battery_pct - battery_needed_pct

    if battery_after < constraints.battery_reserve_pct:
        return [
            ValidationIssue(
                code="INSUFFICIENT_ENDURANCE",
                message=(
                    f"{vehicle_id}: estimated {battery_needed_pct:.1f}% battery needed for "
                    f"{total_m:.0f}m (route + return home); starting at {battery_pct:.0f}% would "
                    f"leave {battery_after:.1f}%, below the {constraints.battery_reserve_pct:.0f}% reserve"
                ),
                vehicle_id=vehicle_id,
            )
        ]
    return []


def assign_altitude_bands(
    vehicle_ids: list[str],
    constraints: SafetyConstraints,
    base_altitude_m: float | None = None,
) -> dict[str, float]:
    """Each vehicle gets its own altitude: base_altitude_m (default
    min_altitude_m) plus altitude_band_spacing_m per vehicle, ordered
    deterministically by id, capped at max_altitude_m. Simple and
    explainable on purpose -- not an optimizer. Used by the planner to
    choose the altitudes it actually puts in each route; the validator
    then checks those real altitudes (check_separation), it never
    trusts this function's output on its own."""
    base = constraints.min_altitude_m if base_altitude_m is None else base_altitude_m
    bands = {}
    for i, vid in enumerate(sorted(vehicle_ids)):
        bands[vid] = min(base + i * constraints.altitude_band_spacing_m, constraints.max_altitude_m)
    return bands


def check_separation(
    routes: dict[str, list[Waypoint]],
    constraints: SafetyConstraints,
) -> list[ValidationIssue]:
    """Pairwise check between every two vehicles' routes, at every
    corresponding waypoint pair (same list index): the two waypoints
    must be at least altitude_band_spacing_m apart vertically OR at
    least min_horizontal_separation_m apart horizontally.

    Uses the altitudes actually written in the routes -- an earlier
    version compared separately-assigned "bands", which a plan could
    pass while its real waypoints sat at the same height.

    Known simplification: "same index" stands in for "same time",
    which assumes routes progress in lockstep. A real check needs
    per-waypoint ETAs. Documented here rather than silently assumed.
    """
    issues: list[ValidationIssue] = []
    vehicle_ids = sorted(routes.keys())
    for i in range(len(vehicle_ids)):
        for j in range(i + 1, len(vehicle_ids)):
            v1, v2 = vehicle_ids[i], vehicle_ids[j]
            r1, r2 = routes[v1], routes[v2]
            for idx in range(min(len(r1), len(r2))):
                alt_gap = abs(r1[idx].alt_m - r2[idx].alt_m)
                if alt_gap >= constraints.altitude_band_spacing_m:
                    continue  # vertically deconflicted at this point
                dist_m = horizontal_distance_m(
                    {"lat": r1[idx].lat, "lon": r1[idx].lon},
                    {"lat": r2[idx].lat, "lon": r2[idx].lon},
                )
                if dist_m < constraints.min_horizontal_separation_m:
                    issues.append(
                        ValidationIssue(
                            code="SEPARATION_CONFLICT",
                            message=(
                                f"{v1} and {v2} are only {dist_m:.1f}m apart at waypoint index {idx} "
                                f"and {alt_gap:.1f}m apart vertically (need "
                                f">={constraints.min_horizontal_separation_m}m horizontal or "
                                f">={constraints.altitude_band_spacing_m}m vertical separation)"
                            ),
                            vehicle_id=v1,
                        )
                    )
    return issues


def validate_mission_plan(
    plan: MissionPlan,
    geofence: Geofence,
    constraints: SafetyConstraints,
    vehicle_homes: dict[str, Waypoint],
    vehicle_battery_pct: dict[str, float],
) -> ValidationResult:
    """The single entry point: every check above, combined. A plan with
    zero issues is valid; otherwise every issue found is returned with
    a clear reason, never just a bare pass/fail."""
    issues: list[ValidationIssue] = []
    routes: dict[str, list[Waypoint]] = {}

    for task in plan.tasks:
        assignment = next((a for a in plan.assignments if a.task_id == task.task_id), None)
        if assignment is None:
            issues.append(
                ValidationIssue(
                    code="UNASSIGNED_TASK",
                    message=f"task {task.task_id} has no vehicle assignment",
                    task_id=task.task_id,
                )
            )
            continue
        routes.setdefault(assignment.vehicle_id, []).extend(task.waypoints)

    # Every vehicle the plan names is checked -- not only those that ended
    # up with a route. A plan must not name (e.g. as coordinator) a drone
    # that is outside the operating area just because it has no waypoints.
    vehicle_ids = sorted(set(routes) | set(plan.roles) | set(plan.mission_request.vehicle_ids))

    for vid in vehicle_ids:
        route = routes.get(vid, [])
        issues += check_altitude(route, constraints, vid)
        issues += check_route_against_fence(route, geofence, vid)

        home = vehicle_homes.get(vid)
        if home is None:
            issues.append(
                ValidationIssue(
                    code="MISSING_HOME_POINT",
                    message=f"no home/launch point known for {vid}",
                    vehicle_id=vid,
                )
            )
            continue
        issues += check_launch_point(home, geofence, vid)
        if not route:
            continue
        battery = vehicle_battery_pct.get(vid)
        if battery is None:
            # Never assume a value we weren't given -- an unknown battery
            # can't pass an endurance check.
            issues.append(
                ValidationIssue(
                    code="MISSING_BATTERY",
                    message=f"battery level unknown for {vid}; can't check endurance",
                    vehicle_id=vid,
                )
            )
        else:
            issues += check_endurance(route, home, constraints, battery, vid)

    issues += check_separation(routes, constraints)

    return ValidationResult(valid=len(issues) == 0, issues=issues)


def make_approval_record(plan: MissionPlan, approved_by: str) -> ApprovalRecord:
    """Records what was approved, by content -- see
    MissionPlan.content_hash()'s docstring for why the hash (not just
    plan_id) is what matters here."""
    return ApprovalRecord(plan_id=plan.plan_id, plan_hash=plan.content_hash(), approved_by=approved_by)


def plan_matches_approval(plan: MissionPlan, approval: ApprovalRecord) -> bool:
    """True only if this exact plan (by content, not just id) is what
    was approved. This is the core of "execution is refused if the
    plan changed after approval" (docs/IMPLEMENTATION_PLAN.md Phase 2)
    -- comparing plan_id alone would let someone swap a plan's
    waypoints post-approval while keeping the same id."""
    return plan.plan_id == approval.plan_id and plan.content_hash() == approval.plan_hash
