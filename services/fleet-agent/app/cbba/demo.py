"""See CBBA working from a terminal. SIMULATION ONLY: no drone, gateway or
API is contacted, and nothing is flown.

    python -m app.cbba.demo                      # 3 drones, everyone can talk
    python -m app.cbba.demo --comms line         # sim-1 <-> sim-2 <-> sim-3 only
    python -m app.cbba.demo --comms split        # sim-3 can't hear anyone
    python -m app.cbba.demo --offline sim-2      # one drone is down
"""
from __future__ import annotations

import argparse
import sys

from app.cbba.models import CommsSpec, DroneHealth, DroneSpec, InspectionPoint, Scenario
from app.cbba.service import allocate
from app.demo import SAMPLE_FENCE, SAMPLE_VEHICLES

CAPABILITIES = {"sim-1": ["camera", "thermal"], "sim-2": ["camera"], "sim-3": ["camera", "thermal"]}

# Places to look at inside the sample geofence near Stanford.
SAMPLE_POINTS = [
    InspectionPoint(point_id="P01-roof-north", lat=37.4302, lon=-122.1712),
    InspectionPoint(point_id="P02-gate", lat=37.4296, lon=-122.1690),
    InspectionPoint(point_id="P03-tower", lat=37.4300, lon=-122.1662, value=150),
    InspectionPoint(point_id="P04-solar-panels", lat=37.4283, lon=-122.1705, required_capability="thermal"),
    InspectionPoint(point_id="P05-fence-east", lat=37.4280, lon=-122.1656),
    InspectionPoint(point_id="P06-pipeline-a", lat=37.4262, lon=-122.1714),
    InspectionPoint(point_id="P07-pipeline-b", lat=37.4258, lon=-122.1693, required_capability="thermal"),
    InspectionPoint(point_id="P08-car-park", lat=37.4260, lon=-122.1668),
    InspectionPoint(point_id="P09-transformer", lat=37.4268, lon=-122.1680, required_capability="thermal", deadline_s=120),
    InspectionPoint(point_id="P10-mast", lat=37.4290, lon=-122.1676, required_capability="lidar"),
    InspectionPoint(point_id="P11-off-site", lat=37.4330, lon=-122.1690),
]

COMMS = {
    "full": CommsSpec(),
    "line": CommsSpec(links=[("sim-1", "sim-2"), ("sim-2", "sim-3")]),
    "split": CommsSpec(links=[("sim-1", "sim-2")]),
}


def sample_scenario(comms: str = "full", offline: list[str] | None = None) -> Scenario:
    drones = [
        DroneSpec(
            drone_id=v.vehicle_id,
            start=v.home,
            battery_pct=v.battery_pct,
            capabilities=CAPABILITIES[v.vehicle_id],
            health=DroneHealth.OFFLINE if v.vehicle_id in (offline or []) else DroneHealth.OK,
        )
        for v in SAMPLE_VEHICLES.values()
    ]
    return Scenario(drones=drones, points=SAMPLE_POINTS, geofence=SAMPLE_FENCE, comms=COMMS[comms])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--comms", choices=sorted(COMMS), default="full")
    parser.add_argument("--offline", action="append", default=[], help="drone id to mark offline (repeatable)")
    args = parser.parse_args(argv)

    scenario = sample_scenario(args.comms, args.offline)
    result = allocate(scenario)
    reference = allocate(scenario, method="SGA")

    print(f"Mode: {result.label}  -- simulation only, nothing is flown")
    print(f"Comms: {args.comms}   Drones: {', '.join(d.drone_id for d in scenario.drones)}")
    for x in result.excluded_drones:
        print(f"  left out {x.item_id}: {x.reason}")
    if result.converged:
        print(f"\nCBBA settled in round {result.last_change_round} (then {result.rounds - result.last_change_round} "
              f"quiet round(s) to confirm), {result.messages_sent} messages")
    else:
        print(f"\nCBBA did NOT converge within {result.rounds} rounds")
    print(f"Drones that can hear each other agree on every winner: {'yes' if result.agreement else 'NO'}")

    print("\nWho inspects what (in flying order):")
    for plan in result.plans:
        caps = "+".join(CAPABILITIES[plan.drone_id])
        if plan.path:
            stops = ", ".join(f"{pid} @{t:.0f}s" for pid, t in zip(plan.path, plan.arrival_s))
            print(f"  {plan.drone_id} [{caps}, {plan.altitude_m:.0f} m]: {stops}")
            print(f"      route {plan.route_m:.0f} m, score {plan.score:.1f}")
        else:
            print(f"  {plan.drone_id} [{caps}]: nothing")
    for x in result.unassigned:
        print(f"  not assigned {x.item_id}: {x.reason}")
    for x in result.excluded_points:
        print(f"  ignored {x.item_id}: {x.reason}")

    same = [(p.drone_id, p.path) for p in result.plans] == [(p.drone_id, p.path) for p in reference.plans]
    print(f"\nTotal score {result.total_score:.1f}; centralised reference (SGA) {reference.total_score:.1f} "
          f"-> same allocation: {'yes' if same else 'no'}")

    if result.safety.valid:
        print("Safety gate: PASSED")
    else:
        print("Safety gate: BLOCKED")
        for issue in result.safety.issues:
            print(f"  - {issue.code}: {issue.message}")
    print(f"Dispatch allowed: {'yes' if result.dispatch_allowed else 'NO'}  (this demo never dispatches anything)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
