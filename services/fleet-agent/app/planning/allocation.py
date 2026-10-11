"""Task allocation by a simple, greedy, sequential auction
(docs/IMPLEMENTATION_PLAN.md Phase 3).

How it works, in plain terms:
  1. Every drone that has enough battery is a bidder.
  2. Each round, every bidder bids on every cell still unassigned.
     A bid is a cost -- lower is better:
         cost = (distance already committed + distance to this cell)
                * (100 / battery %)
     "Distance already committed" is how far the drone has already
     promised to fly in earlier rounds, so a drone that has already
     won lots of work bids higher -- that is what spreads the work out
     fairly instead of the nearest drone winning everything. Dividing
     by battery % makes a drone with less charge bid higher.
  3. The single cheapest bid of the round wins: that drone gets that
     cell, and its "position" moves to the cell for its next bids.
  4. Repeat until every cell is assigned.

Ties are broken by vehicle id, then cell id, so the same inputs always
give the same result -- an auction that can't be reproduced can't be
tested or explained.

This is a single-process function. In Phase 4 the same rules run as
messages between drone agents; the arithmetic doesn't change.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.models import VehicleRole, Waypoint
from app.planning.coverage import Cell
from app.safety.geofence import horizontal_distance_m


@dataclass(frozen=True)
class VehicleState:
    vehicle_id: str
    home: Waypoint
    battery_pct: float


@dataclass
class Award:
    cell: Cell
    vehicle_id: str
    bid_cost: float
    leg_m: float  # distance from the vehicle's previous position to this cell


@dataclass
class AuctionResult:
    awards: list[Award] = field(default_factory=list)  # in the order they were won
    excluded: dict[str, str] = field(default_factory=dict)  # vehicle_id -> reason

    def cells_for(self, vehicle_id: str) -> list[Award]:
        return [a for a in self.awards if a.vehicle_id == vehicle_id]


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return horizontal_distance_m({"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]})


def auction_cells(
    cells: list[Cell],
    vehicles: list[VehicleState],
    min_battery_pct: float,
    cell_work_m: dict[str, float],
) -> AuctionResult:
    """cell_work_m: how far it takes to sweep each cell (its lawnmower
    path length), added to the winner's committed distance. Required on
    purpose: without it, winning a cell costs only the short hop to it,
    so one drone can chain neighbouring cells and the split is unfair."""
    missing = [c.cell_id for c in cells if c.cell_id not in cell_work_m]
    if missing:
        raise ValueError(f"cell_work_m is missing cells: {missing}")
    result = AuctionResult()
    bidders = []
    for v in sorted(vehicles, key=lambda v: v.vehicle_id):
        if v.battery_pct <= min_battery_pct:
            result.excluded[v.vehicle_id] = (
                f"battery {v.battery_pct:.0f}% is at or below the {min_battery_pct:.0f}% "
                "minimum to accept new work"
            )
        else:
            bidders.append(v)
    if not bidders:
        return result

    position = {v.vehicle_id: (v.home.lat, v.home.lon) for v in bidders}
    committed_m = {v.vehicle_id: 0.0 for v in bidders}
    remaining = sorted(cells, key=lambda c: c.cell_id)

    while remaining:
        best = None
        for v in bidders:
            for cell in remaining:
                leg = _dist(position[v.vehicle_id], cell.centroid)
                cost = (committed_m[v.vehicle_id] + leg) * (100.0 / v.battery_pct)
                key = (round(cost, 6), v.vehicle_id, cell.cell_id)
                if best is None or key < best[0]:
                    best = (key, v, cell, leg, cost)

        _, winner, cell, leg, cost = best
        result.awards.append(Award(cell=cell, vehicle_id=winner.vehicle_id, bid_cost=round(cost, 3), leg_m=leg))
        committed_m[winner.vehicle_id] += leg + cell_work_m[cell.cell_id]
        position[winner.vehicle_id] = cell.centroid
        remaining.remove(cell)

    return result


def assign_roles(bidder_ids: list[str]) -> dict[str, VehicleRole]:
    """The lowest-id bidder is the COORDINATOR (it runs the auction --
    and still searches); everyone else is a SCOUT. Deterministic, so
    every agent would independently agree on who coordinates (that
    matters for Phase 4's leader election)."""
    ordered = sorted(bidder_ids)
    return {vid: (VehicleRole.COORDINATOR if i == 0 else VehicleRole.SCOUT) for i, vid in enumerate(ordered)}
