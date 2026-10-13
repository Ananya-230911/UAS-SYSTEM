"""Sequential Greedy Algorithm (SGA) -- the centralised reference answer.

One "central" loop that sees every drone at once. Each step:
  - every drone that still has room names the free point with its highest
    true marginal gain, and its bid for it -- warped exactly as in CBBA
    (capped at the drone's earlier bids, see CbbaAgent.warp_cap);
  - the highest bid wins (tie: lower drone id); that point is assigned.
Repeat until nobody can add a point.

Same scoring, insertion rule, bid warping and tie breaks as CBBA. With a
fully connected comms graph, CBBA's decentralised rounds should land on
exactly this allocation (paper, Theorem 1, applied to the warped bids),
so SGA is the oracle CBBA is tested against. It never dispatches
anything. (The existing app/planning/allocation.py auction is a
coverage-cell auction with a different cost, so it can't serve as this
oracle; it stays untouched.)
"""
from __future__ import annotations

from app.cbba.models import CbbaConfig, DroneSpec, InspectionPoint
from app.cbba.scoring import BID_EPS, best_insertion, evaluate_path
from app.core.models import SafetyConstraints


def run_sga(
    drones: list[DroneSpec],
    points: list[InspectionPoint],
    constraints: SafetyConstraints,
    config: CbbaConfig,
    altitudes: dict[str, float],
) -> dict[str, tuple[list[str], list[str]]]:
    """Returns drone_id -> (path, bundle)."""
    by_id = {p.point_id: p for p in points}
    drones = sorted(drones, key=lambda d: d.drone_id)
    paths: dict[str, list[str]] = {d.drone_id: [] for d in drones}
    bundles: dict[str, list[str]] = {d.drone_id: [] for d in drones}
    bids: dict[str, list[float]] = {d.drone_id: [] for d in drones}
    free = sorted(by_id)

    while free:
        best: tuple[float, str, str, int] | None = None  # (bid, drone, point, position)
        for d in drones:  # sorted by id: strict ">" below gives ties to the lower id
            did = d.drone_id
            if len(bundles[did]) >= config.max_bundle_size:
                continue
            path_pts = [by_id[pid] for pid in paths[did]]
            ev = evaluate_path(d, path_pts, constraints, config, altitudes[did])
            current = ev.score if ev else 0.0
            choice: tuple[float, str, int] | None = None  # this drone's best (gain, point, position)
            for pid in free:
                ins = best_insertion(d, path_pts, current, by_id[pid], constraints, config, altitudes[did])
                if ins is None or ins[0] <= BID_EPS:
                    continue
                if choice is None or ins[0] > choice[0] + BID_EPS:
                    choice = (ins[0], pid, ins[1])
            if choice is None:
                continue
            bid = min(choice[0], min(bids[did], default=float("inf")))
            if best is None or bid > best[0] + BID_EPS:
                best = (bid, did, choice[1], choice[2])
        if best is None:
            break
        bid, did, pid, pos = best
        paths[did].insert(pos, pid)
        bundles[did].append(pid)
        bids[did].append(bid)
        free.remove(pid)

    return {did: (paths[did], bundles[did]) for did in paths}
