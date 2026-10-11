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


def assign_altitude_bands(vehicle_ids: list[str], constraints: SafetyConstraints) -> dict[str, float]:
    """Each vehicle gets its own altitude: min_altitude_m plus
    altitude_band_spacing_m per vehicle (ordered deterministically by
    id), capped at max_altitude_m. Simple and explainable on purpose --
    not an optimizer; good enough to vertically deconflict a small
    fleet."""
    bands = {}
    for i, vid in enumerate(sorted(vehicle_ids)):
        alt = constraints.min_altitude_m + i * constraints.altitude_band_spacing_m
        bands[vid] = min(alt, constraints.max_altitude_m)
    return bands


def check_separation(
    routes: dict[str, list[Waypoint]],
    altitude_bands: dict[str, float],
    constraints: SafetyConstraints,
) -> list[ValidationIssue]:
    """Pairwise check between every two vehicles' routes. If their
    assigned altitude bands are already far enough apart
    (altitude_band_spacing_m), they're vertically deconflicted
    regardless of horizontal distance. Otherwise every corresponding
    waypoint pair (same list index) must be at least
    min_horizontal_separation_m apart.

    Known simplification: using "same index" as a stand-in for "same
    time" assumes both routes progress in lockstep -- a real check
    would need per-waypoint ETAs, which Phase 3's coverage/allocation
    (not built yet) doesn't compute. Documented here rather than
    silently assumed.
    """
    issues: list[ValidationIssue] = []
    vehicle_ids = sorted(routes.keys())
    for i in range(len(vehicle_ids)):
        for j in range(i + 1, len(vehicle_ids)):
            v1, v2 = vehicle_ids[i], vehicle_ids[j]
            alt_gap = abs(altitude_bands.get(v1, 0.0) - altitude_bands.get(v2, 0.0))
            if alt_gap >= constraints.altitude_band_spacing_m:
                continue  # vertically deconflicted already

            r1, r2 = routes[v1], routes[v2]
            for idx in range(min(len(r1), len(r2))):
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
                                f"with altitude bands {alt_gap:.1f}m apart (need "
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

    vehicle_ids = sorted(routes.keys())
    altitude_bands = assign_altitude_bands(vehicle_ids, constraints)

    for vid in vehicle_ids:
        route = routes[vid]
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
        else:
            issues += check_launch_point(home, geofence, vid)
            battery = vehicle_battery_pct.get(vid, 100.0)
            issues += check_endurance(route, home, constraints, battery, vid)

    issues += check_separation(routes, altitude_bands, constraints)

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
