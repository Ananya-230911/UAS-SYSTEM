"""Shared builders for the CBBA tests."""
from __future__ import annotations

import itertools
import random

from app.cbba.models import CbbaConfig, CommsSpec, DroneSpec, InspectionPoint, Scenario
from app.core.models import Geofence, GeofencePoint, Waypoint

# ~780 m x 620 m box near Stanford (same as app/demo.py's sample fence).
FENCE = Geofence(
    points=[
        GeofencePoint(lat=37.4250, lon=-122.1720),
        GeofencePoint(lat=37.4250, lon=-122.1650),
        GeofencePoint(lat=37.4310, lon=-122.1650),
        GeofencePoint(lat=37.4310, lon=-122.1720),
    ]
)


def drone(drone_id: str, lat: float = 37.4275, lon: float = -122.1690, battery: float = 90.0, caps=("camera",), **kw) -> DroneSpec:
    return DroneSpec(
        drone_id=drone_id,
        start=Waypoint(lat=lat, lon=lon, alt_m=0.0),
        battery_pct=battery,
        capabilities=list(caps),
        **kw,
    )


def point(point_id: str, lat: float, lon: float, **kw) -> InspectionPoint:
    return InspectionPoint(point_id=point_id, lat=lat, lon=lon, **kw)


def scenario(drones, points, **kw) -> Scenario:
    return Scenario(drones=drones, points=points, geofence=FENCE, **kw)


def random_scenario(rng: random.Random, connected_only: bool) -> Scenario:
    """1-6 drones, 1-18 points with random sensors, values, deadlines,
    hover times, batteries and bundle limits. Comms either fully
    connected, or a random connected multi-hop graph (spanning tree plus
    a few extra links)."""
    n_drones = rng.randint(1, 6)
    n_points = rng.randint(1, 18)
    lat = lambda: rng.uniform(37.4252, 37.4308)  # noqa: E731
    lon = lambda: rng.uniform(-122.1718, -122.1652)  # noqa: E731
    drones = [
        drone(f"d{i}", lat(), lon(), rng.uniform(25, 100), rng.sample(["camera", "thermal"], rng.randint(0, 2)))
        for i in range(n_drones)
    ]
    points = [
        point(
            f"p{j:02d}",
            lat(),
            lon(),
            value=rng.choice([50, 100, 100, 150]),
            required_capability=rng.choice([None, None, "camera", "thermal"]),
            inspect_duration_s=rng.choice([0, 0, 10]),
            deadline_s=rng.choice([None, None, 60, 200]),
        )
        for j in range(n_points)
    ]
    ids = [d.drone_id for d in drones]
    if connected_only:
        comms = CommsSpec()
    else:
        order = ids[:]
        rng.shuffle(order)
        links = [(order[i], order[rng.randrange(i)]) for i in range(1, len(order))]
        links += [pair for pair in itertools.combinations(ids, 2) if rng.random() < 0.2]
        comms = CommsSpec(links=links)
    config = CbbaConfig(max_bundle_size=rng.randint(1, 6), discount_per_s=rng.choice([0.99, 0.995, 0.999]))
    return scenario(drones, points, comms=comms, config=config)


def assignment(result) -> list[tuple[str, list[str]]]:
    return [(p.drone_id, p.path) for p in result.plans]
