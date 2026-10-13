"""The deterministic safety gate every allocation passes before dispatch
(docs/CBBA_PLAN.md section 3, rule 2).

Reuses app/safety/validator.py's checks -- altitude, every waypoint and
route segment inside the geofence (concave fences included), launch
point, endurance with return home and reserve, separation -- rather
than re-implementing them. Adds only what CBBA results need on top:
convergence, no point given to two drones, the drone carries the
required sensor, the drone is known and not offline, and deadlines
(recomputed here independently of the bidding code).

Any issue blocks dispatch. Nothing -- operator, CrewAI, or any LLM --
can override a failed check; the only way forward is new inputs and a
new allocation.
"""
from __future__ import annotations

from app.cbba.models import DroneHealth, DronePlan, DroneSpec, InspectionPoint
from app.core.models import Geofence, SafetyConstraints, ValidationIssue, ValidationResult, Waypoint
from app.safety.geofence import horizontal_distance_m
from app.cbba.scoring import endurance_issues
from app.safety.validator import (
    check_altitude,
    check_launch_point,
    check_route_against_fence,
    check_separation,
)


def _issue(code: str, message: str, vehicle_id: str | None = None, task_id: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, vehicle_id=vehicle_id, task_id=task_id)


def check_allocation(
    plans: list[DronePlan],
    drones: dict[str, DroneSpec],
    points: dict[str, InspectionPoint],
    geofence: Geofence,
    constraints: SafetyConstraints,
    converged: bool,
) -> ValidationResult:
    issues: list[ValidationIssue] = []

    if not converged:
        issues.append(_issue("NOT_CONVERGED", "the drones did not reach agreement; nothing may be dispatched"))

    owner: dict[str, str] = {}
    routes: dict[str, list[Waypoint]] = {}

    for plan in sorted(plans, key=lambda p: p.drone_id):
        did = plan.drone_id
        drone = drones.get(did)
        if drone is None:
            issues.append(_issue("UNKNOWN_DRONE", f"{did} is not a drone in this scenario", did))
            continue
        if not plan.path:
            continue
        if drone.health == DroneHealth.OFFLINE:
            issues.append(_issue("DRONE_OFFLINE", f"{did} is offline but has points assigned", did))

        known_path: list[InspectionPoint] = []
        for pid in plan.path:
            point = points.get(pid)
            if point is None:
                issues.append(_issue("UNKNOWN_POINT", f"{did} is assigned unknown point {pid}", did, pid))
                continue
            if pid in owner:
                issues.append(
                    _issue("DUPLICATE_ASSIGNMENT", f"point {pid} is assigned to both {owner[pid]} and {did}", did, pid)
                )
            else:
                owner[pid] = did
            if point.required_capability and point.required_capability not in drone.capabilities:
                issues.append(
                    _issue(
                        "CAPABILITY_MISMATCH",
                        f"{did} has no '{point.required_capability}' sensor, needed for {pid}",
                        did,
                        pid,
                    )
                )
            known_path.append(point)

        route = [Waypoint(lat=p.lat, lon=p.lon, alt_m=plan.altitude_m) for p in known_path]
        here = Waypoint(lat=drone.start.lat, lon=drone.start.lon, alt_m=plan.altitude_m)
        home_pt = drone.home_point()
        home = Waypoint(lat=home_pt.lat, lon=home_pt.lon, alt_m=plan.altitude_m)
        routes[did] = route

        issues += check_altitude(route, constraints, did)
        # Include the legs from where it is and back home: they must stay inside too.
        issues += check_route_against_fence([here] + route + [home], geofence, did)
        issues += check_launch_point(home_pt, geofence, did)
        issues += endurance_issues(drone, route, constraints)
        issues += _check_deadlines(drone, known_path, constraints)

    # Lock-step separation check (known limitation, see check_separation's
    # docstring). Each drone has its own altitude band, so this mainly
    # catches two drones placed at the same height.
    issues += check_separation(routes, constraints)

    return ValidationResult(valid=not issues, issues=issues)


def _check_deadlines(
    drone: DroneSpec, path: list[InspectionPoint], constraints: SafetyConstraints
) -> list[ValidationIssue]:
    issues = []
    t = 0.0
    lat, lon = drone.start.lat, drone.start.lon
    for p in path:
        t += horizontal_distance_m({"lat": lat, "lon": lon}, {"lat": p.lat, "lon": p.lon}) / constraints.cruise_speed_ms
        if p.deadline_s is not None and t > p.deadline_s:
            issues.append(
                _issue(
                    "DEADLINE_MISSED",
                    f"{drone.drone_id} reaches {p.point_id} after {t:.0f}s; deadline is {p.deadline_s:.0f}s",
                    drone.drone_id,
                    p.point_id,
                )
            )
        t += p.inspect_duration_s
        lat, lon = p.lat, p.lon
    return issues
