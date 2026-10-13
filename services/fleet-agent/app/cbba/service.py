"""The one public entry point: allocate(scenario) -> CbbaResult.

Steps, all deterministic:
  1. Leave out drones that are offline or start outside the geofence,
     and points outside the geofence -- each with a stated reason.
  2. Give each remaining drone its own altitude band
     (app/safety/validator.py::assign_altitude_bands).
  3. Run CBBA (or SGA, the centralised reference) over what's left.
  4. Pass the result through the safety gate. dispatch_allowed is True
     only if CBBA converged AND every safety check passed.

Later phases (the backend, CrewAI tools, the dashboard) call this and
read its result; nothing outside this package computes or edits bids,
winners, bundles or routes (docs/CBBA_PLAN.md rule 1).
"""
from __future__ import annotations

from app.cbba.models import (
    SIM_LABEL,
    CbbaResult,
    CommsSpec,
    DroneHealth,
    DronePlan,
    DroneSpec,
    ExcludedItem,
    InspectionPoint,
    Scenario,
)
from app.cbba.network import CommsGraph
from app.cbba.runner import run_cbba
from app.cbba.safety_gate import check_allocation
from app.cbba.scoring import can_do, evaluate_path
from app.cbba.sga import run_sga
from app.core.models import ValidationIssue, Waypoint
from app.safety.geofence import point_in_polygon
from app.safety.validator import assign_altitude_bands

SGA_LABEL = "SGA (centralised reference, not dispatched)"


def _inside(lat: float, lon: float, scenario: Scenario) -> bool:
    return point_in_polygon(lat, lon, [p.model_dump() for p in scenario.geofence.points])


def _split_inputs(scenario: Scenario):
    drones, excluded_drones = [], []
    for d in sorted(scenario.drones, key=lambda d: d.drone_id):
        if d.health == DroneHealth.OFFLINE:
            excluded_drones.append(ExcludedItem(item_id=d.drone_id, reason="offline"))
        elif not _inside(d.start.lat, d.start.lon, scenario):
            excluded_drones.append(ExcludedItem(item_id=d.drone_id, reason="starts outside the geofence"))
        else:
            drones.append(d)

    points, excluded_points = [], []
    for p in sorted(scenario.points, key=lambda p: p.point_id):
        if _inside(p.lat, p.lon, scenario):
            points.append(p)
        else:
            excluded_points.append(ExcludedItem(item_id=p.point_id, reason="outside the geofence"))
    return drones, excluded_drones, points, excluded_points


def _live_links(scenario: Scenario, drones: list[DroneSpec]) -> CommsSpec:
    """A drone that takes no part (offline, or outside the fence) relays
    nothing: drop its links. Taking out a relay can split the network."""
    if scenario.comms.links is None:
        return scenario.comms
    active = {d.drone_id for d in drones}
    return CommsSpec(links=[(a, b) for a, b in scenario.comms.links if a in active and b in active])


def _drone_plan(drone: DroneSpec, path: list[str], bundle: list[str], by_id, scenario: Scenario, alt: float) -> DronePlan:
    pts = [by_id[pid] for pid in path]
    ev = evaluate_path(drone, pts, scenario.constraints, scenario.config, alt)
    home = Waypoint(lat=drone.start.lat, lon=drone.start.lon, alt_m=alt)
    route = [home] + [Waypoint(lat=p.lat, lon=p.lon, alt_m=alt) for p in pts] + [home] if pts else []
    return DronePlan(
        drone_id=drone.drone_id,
        altitude_m=alt,
        path=list(path),
        bundle=list(bundle),
        # An infeasible path can't come out of CBBA/SGA; if it ever did,
        # report zero score and let the safety gate name the problem.
        arrival_s=[round(t, 1) for t in ev.arrival_s] if ev else [],
        score=round(ev.score, 4) if ev else 0.0,
        route_m=round(ev.route_m, 1) if ev else 0.0,
        route=route,
    )


def _unassigned_reason(point: InspectionPoint, drones: list[DroneSpec], scenario: Scenario, altitudes) -> str:
    capable = [d for d in drones if can_do(d, point)]
    if not capable:
        return f"no available drone carries a '{point.required_capability}' sensor"
    if not any(
        evaluate_path(d, [point], scenario.constraints, scenario.config, altitudes[d.drone_id]) for d in capable
    ):
        return "out of reach: no capable drone can get there (and back with its battery reserve) in time"
    return "every capable drone was full (bundle limit) or adding it would break a deadline or the battery reserve"


def allocate(scenario: Scenario, method: str = "CBBA") -> CbbaResult:
    if method not in ("CBBA", "SGA"):
        raise ValueError("method must be 'CBBA' or 'SGA'")
    drones, excluded_drones, points, excluded_points = _split_inputs(scenario)
    altitudes = assign_altitude_bands([d.drone_id for d in drones], scenario.constraints, scenario.base_altitude_m)
    by_id = {p.point_id: p for p in points}

    if method == "CBBA" and drones:
        graph = CommsGraph.from_spec(_live_links(scenario, drones), drones)
        run = run_cbba(drones, points, scenario.constraints, scenario.config, graph, altitudes)
        paths = {aid: (a.path, a.bundle) for aid, a in run.agents.items()}
        converged, agreement, label = run.converged, run.agreement, SIM_LABEL
        groups = graph.components()
        rounds, last_change, messages = run.rounds, run.last_change_round, run.messages_sent
    else:
        paths = run_sga(drones, points, scenario.constraints, scenario.config, altitudes) if drones else {}
        converged, agreement = bool(drones), True
        groups = [[d.drone_id for d in drones]] if drones else []
        rounds, last_change, messages = 0, 0, 0
        label = SGA_LABEL if method == "SGA" else SIM_LABEL

    drone_by_id = {d.drone_id: d for d in drones}
    plans = [
        _drone_plan(drone_by_id[did], path, bundle, by_id, scenario, altitudes[did])
        for did, (path, bundle) in sorted(paths.items())
    ]

    winners: dict[str, str | None] = {pid: None for pid in sorted(by_id)}
    for plan in plans:
        for pid in plan.path:
            if winners[pid] is None:
                winners[pid] = plan.drone_id  # a second claim is caught by the safety gate

    unassigned = [
        ExcludedItem(item_id=pid, reason=_unassigned_reason(by_id[pid], drones, scenario, altitudes))
        for pid, w in winners.items()
        if w is None
    ]

    safety = check_allocation(
        plans,
        {d.drone_id: d for d in scenario.drones},
        {p.point_id: p for p in scenario.points},
        scenario.geofence,
        scenario.constraints,
        converged,
    )
    if not drones:
        safety.issues.append(ValidationIssue(code="NO_AVAILABLE_DRONES", message="no drone is available to take part"))
        safety.valid = False

    return CbbaResult(
        label=label,
        method=method,
        converged=converged,
        rounds=rounds,
        last_change_round=last_change,
        messages_sent=messages,
        agreement=agreement,
        comms_groups=groups,
        plans=plans,
        winners=winners,
        total_score=round(sum(p.score for p in plans), 4),
        unassigned=unassigned,
        excluded_drones=excluded_drones,
        excluded_points=excluded_points,
        safety=safety,
        dispatch_allowed=converged and safety.valid,
    )

