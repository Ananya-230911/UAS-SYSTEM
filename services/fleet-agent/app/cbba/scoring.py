"""How much a drone's path is worth, and what one more point adds to it.

Score of a path (Choi, Brunet & How 2009, eq. 11, time-discounted reward):

    S(path) = sum over points p in path of  value_p * lambda ** arrival_time_p

Flying to a point later makes it worth less, so adding a point to a path
can only delay the points after it. That makes the marginal gain of any
point shrink (never grow) as a path gets longer -- the "diminishing
marginal gain" property CBBA needs to converge (paper, section IV).

A path is infeasible if a point's deadline is missed, or if flying it
(plus returning home) would take the battery below the reserve. The
endurance check is app/safety/validator.py::check_endurance itself --
the same function the safety gate later uses -- so bidding can never be
more optimistic than the check that blocks dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.cbba.models import CbbaConfig, DroneSpec, InspectionPoint
from app.core.models import SafetyConstraints, Waypoint
from app.safety.geofence import horizontal_distance_m
from app.safety.validator import check_endurance

# Bids closer than this count as equal, so float noise can't decide a tie.
BID_EPS = 1e-9


@dataclass(frozen=True)
class PathEval:
    score: float
    arrival_s: list[float]
    route_m: float  # start -> points -> start


def _dist(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    return horizontal_distance_m({"lat": a_lat, "lon": a_lon}, {"lat": b_lat, "lon": b_lon})


def evaluate_path(
    drone: DroneSpec,
    path: list[InspectionPoint],
    constraints: SafetyConstraints,
    config: CbbaConfig,
    altitude_m: float,
) -> PathEval | None:
    """Score of flying `path` in order from the drone's start, or None if
    the path is infeasible (deadline missed or battery reserve broken)."""
    t = 0.0
    score = 0.0
    arrivals = []
    lat, lon = drone.start.lat, drone.start.lon
    leg_total = 0.0
    for p in path:
        leg = _dist(lat, lon, p.lat, p.lon)
        leg_total += leg
        t += leg / constraints.cruise_speed_ms
        if p.deadline_s is not None and t > p.deadline_s:
            return None
        arrivals.append(t)
        score += p.value * config.discount_per_s ** t
        t += p.inspect_duration_s
        lat, lon = p.lat, p.lon
    leg_total += _dist(lat, lon, drone.start.lat, drone.start.lon)

    if path:
        route = route_waypoints(path, altitude_m)
        if check_endurance(route, drone.start, constraints, drone.battery_pct, drone.drone_id):
            return None
    return PathEval(score=score, arrival_s=arrivals, route_m=leg_total)


def route_waypoints(path: list[InspectionPoint], altitude_m: float) -> list[Waypoint]:
    return [Waypoint(lat=p.lat, lon=p.lon, alt_m=altitude_m) for p in path]


def can_do(drone: DroneSpec, point: InspectionPoint) -> bool:
    return point.required_capability is None or point.required_capability in drone.capabilities


def best_insertion(
    drone: DroneSpec,
    path: list[InspectionPoint],
    current_score: float,
    point: InspectionPoint,
    constraints: SafetyConstraints,
    config: CbbaConfig,
    altitude_m: float,
) -> tuple[float, int] | None:
    """The most `point` can add to `path` and where to insert it, or None
    if the drone can't do it or no position keeps the path feasible.
    Earliest position wins a tie, so the result is deterministic."""
    if not can_do(drone, point):
        return None
    best: tuple[float, int] | None = None
    for pos in range(len(path) + 1):
        trial = path[:pos] + [point] + path[pos:]
        ev = evaluate_path(drone, trial, constraints, config, altitude_m)
        if ev is None:
            continue
        gain = ev.score - current_score
        if best is None or gain > best[0] + BID_EPS:
            best = (gain, pos)
    return best


def outbids(bid_a: float, agent_a: str, bid_b: float, agent_b: str | None) -> bool:
    """Does (bid_a by agent_a) beat (bid_b by agent_b)? Higher bid wins;
    an equal bid goes to the lower drone id. Nobody (None) is beaten by
    any positive bid."""
    if agent_b is None:
        return bid_a > BID_EPS
    if bid_a > bid_b + BID_EPS:
        return True
    if abs(bid_a - bid_b) <= BID_EPS:
        return agent_a < agent_b
    return False
