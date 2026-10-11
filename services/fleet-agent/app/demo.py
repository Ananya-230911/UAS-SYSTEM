"""See Phase 3 working from a terminal: operator text -> plan -> safety
validation -> approval. READ-ONLY: it never sends a command to any drone
(flying an approved plan is Phase 5).

    # built-in sample: 3 drones and a square geofence near Stanford
    python -m app.demo "search the area inside the geofence with 3 drones"

    # your live fleet (run_fleet.ps1 / run_local.ps1 must be running, and
    # a geofence drawn and saved in the GCS for at least one drone)
    python -m app.demo "search the area with 3 drones at 25 m" --api http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from app.core.models import Geofence, GeofencePoint, SafetyConstraints, SearchArea, Waypoint
from app.mission.goal_parser import NeedsClarification, parse_goal
from app.mission.manager import MissionManager
from app.mission.planner import plan_mission
from app.planning.allocation import VehicleState

FRESH_SECONDS = 15  # same threshold the GCS uses for "DISCONNECTED"

# Matches scripts/run_fleet.ps1's default spacing around sim.py's home.
SAMPLE_VEHICLES = {
    "sim-1": VehicleState("sim-1", Waypoint(lat=37.4275, lon=-122.1697, alt_m=0.0), 96.0),
    "sim-2": VehicleState("sim-2", Waypoint(lat=37.4275, lon=-122.1684, alt_m=0.0), 92.0),
    "sim-3": VehicleState("sim-3", Waypoint(lat=37.4275, lon=-122.1671, alt_m=0.0), 61.0),
}
SAMPLE_FENCE = Geofence(
    points=[
        GeofencePoint(lat=37.4250, lon=-122.1720),
        GeofencePoint(lat=37.4250, lon=-122.1650),
        GeofencePoint(lat=37.4310, lon=-122.1650),
        GeofencePoint(lat=37.4310, lon=-122.1720),
    ]
)


def _get(api: str, path: str, api_key: str | None):
    req = urllib.request.Request(api.rstrip("/") + path)
    if api_key:
        req.add_header("X-API-Key", api_key)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _parse_time(value: str) -> datetime:
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo; it's UTC


def load_live_fleet(api: str, api_key: str | None) -> tuple[dict[str, VehicleState], Geofence | None, list[str]]:
    """Fresh vehicles' current position/battery and the first saved
    geofence found. Vehicles whose telemetry is stale or missing a
    position/battery are skipped and reported -- never filled in."""
    notes = []
    vehicles: dict[str, VehicleState] = {}
    fence = None
    now = datetime.now(timezone.utc)
    for v in sorted(_get(api, "/vehicles", api_key), key=lambda v: v["vehicle_id"]):
        vid = v["vehicle_id"]
        age = (now - _parse_time(v["last_seen"])).total_seconds()
        if age > FRESH_SECONDS:
            notes.append(f"skipped {vid}: last telemetry {age:.0f}s ago (disconnected)")
            continue
        samples = _get(api, f"/vehicles/{vid}/telemetry?limit=1", api_key)
        s = samples[-1] if samples else {}
        if s.get("lat") is None or s.get("lon") is None or s.get("battery_pct") is None:
            notes.append(f"skipped {vid}: no position/battery reported yet")
            continue
        vehicles[vid] = VehicleState(vid, Waypoint(lat=s["lat"], lon=s["lon"], alt_m=0.0), float(s["battery_pct"]))
        if fence is None:
            try:
                fence = Geofence(points=_get(api, f"/vehicles/{vid}/geofence", api_key)["points"])
                notes.append(f"using the geofence saved for {vid}")
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
    return vehicles, fence, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("goal", help='e.g. "search the area inside the geofence with 3 drones"')
    ap.add_argument("--api", help="services/api base URL to read the live fleet from (read-only)")
    ap.add_argument("--api-key", help="only if services/api was started with API_KEY")
    ap.add_argument("--operator", default="operator", help="name recorded on the approval")
    args = ap.parse_args(argv)

    if args.api:
        try:
            vehicles, fence, notes = load_live_fleet(args.api, args.api_key)
        except (urllib.error.URLError, OSError) as exc:
            print(f"Could not reach {args.api}: {exc}. Is the fleet running?")
            return 2
        source = f"live fleet at {args.api}"
    else:
        vehicles, fence, notes = SAMPLE_VEHICLES, SAMPLE_FENCE, []
        source = "built-in sample (3 drones near Stanford)"

    print(f"Data source: {source}")
    for n in notes:
        print(f"  - {n}")
    print(f"Drones available: {', '.join(sorted(vehicles)) or 'none'}")
    print(f'\nOperator: "{args.goal}"')

    try:
        parsed = parse_goal(
            args.goal,
            available_vehicle_ids=list(vehicles),
            search_area=SearchArea(points=fence.points) if fence else None,
        )
    except NeedsClarification as q:
        print(f"\nAgent asks: {q.question}")
        print("(No plan was made and no command was sent.)")
        return 1

    req = parsed.request
    print(f"Interpreted by: {parsed.provider}")
    print(f"  -> search with {len(req.vehicle_ids)} drone(s) {req.vehicle_ids} at {req.altitude_m:.0f} m")

    constraints = SafetyConstraints()
    result = plan_mission(req, fence, constraints, vehicles)

    print("\nPlan:")
    for line in result.explanations:
        print(f"  - {line}")

    if result.plan is None:
        print("\nNo plan could be built:")
        for issue in result.validation.issues:
            print(f"  x [{issue.code}] {issue.message}")
        return 1

    manager = MissionManager(result.plan)
    homes = {vid: vehicles[vid].home for vid in result.plan.mission_request.vehicle_ids}
    battery = {vid: vehicles[vid].battery_pct for vid in homes}
    validation = manager.validate(fence, constraints, homes, battery)

    n_wps = sum(len(t.waypoints) for t in result.plan.tasks)
    print(f"\nSafety validation: {'PASSED' if validation.valid else 'FAILED'} "
          f"({len(result.plan.tasks)} tasks, {n_wps} waypoints checked)")
    for issue in validation.issues[:15]:
        print(f"  x [{issue.code}] {issue.message}")
    if len(validation.issues) > 15:
        print(f"  ... and {len(validation.issues) - 15} more")

    if validation.valid:
        approval = manager.approve(args.operator)
        print(f"Approved by {approval.approved_by}; plan fingerprint {approval.plan_hash[:16]}...")

    print(f"\nMission state: {manager.status.value}")
    print("History: " + " -> ".join(e.event_type.replace("MISSION_", "") for e in manager.events))
    print("No flight commands were sent (executing an approved plan is Phase 5).")
    return 0 if validation.valid else 1


if __name__ == "__main__":
    sys.exit(main())
