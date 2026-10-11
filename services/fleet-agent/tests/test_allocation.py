import pytest

from app.core.models import GeofencePoint, SearchArea, VehicleRole, Waypoint
from app.planning.allocation import VehicleState, assign_roles, auction_cells
from app.planning.coverage import lawnmower_path, path_length_m, split_into_cells

AREA = SearchArea(
    points=[
        GeofencePoint(lat=37.4250, lon=-122.1720),
        GeofencePoint(lat=37.4250, lon=-122.1650),
        GeofencePoint(lat=37.4310, lon=-122.1650),
        GeofencePoint(lat=37.4310, lon=-122.1720),
    ]
)
HOME = Waypoint(lat=37.4280, lon=-122.1685, alt_m=0.0)


def _fleet(*batteries):
    return [VehicleState(f"sim-{i + 1}", HOME, b) for i, b in enumerate(batteries)]


def _work(cells):
    return {c.cell_id: path_length_m(lawnmower_path(c, 18.5, 20.0)) for c in cells}


def test_every_cell_is_assigned_exactly_once():
    cells = split_into_cells(AREA, 6)
    result = auction_cells(cells, _fleet(90, 90, 90), min_battery_pct=20, cell_work_m=_work(cells))
    assigned = [a.cell.cell_id for a in result.awards]
    assert sorted(assigned) == sorted(c.cell_id for c in cells)


def test_equal_drones_get_a_fair_share():
    cells = split_into_cells(AREA, 6)
    result = auction_cells(cells, _fleet(90, 90, 90), min_battery_pct=20, cell_work_m=_work(cells))
    counts = [len(result.cells_for(f"sim-{i}")) for i in (1, 2, 3)]
    assert counts == [2, 2, 2]


def test_cell_work_is_required_so_the_split_cannot_silently_be_unfair():
    cells = split_into_cells(AREA, 6)
    with pytest.raises(ValueError, match="missing cells"):
        auction_cells(cells, _fleet(90, 90, 90), min_battery_pct=20, cell_work_m={})


def test_auction_is_deterministic():
    cells = split_into_cells(AREA, 6)
    work = _work(cells)
    a = auction_cells(cells, _fleet(90, 80, 70), min_battery_pct=20, cell_work_m=work)
    b = auction_cells(list(reversed(cells)), list(reversed(_fleet(90, 80, 70))), min_battery_pct=20, cell_work_m=work)
    assert [(x.vehicle_id, x.cell.cell_id) for x in a.awards] == [(x.vehicle_id, x.cell.cell_id) for x in b.awards]


def test_lower_battery_wins_less_work_than_a_full_one():
    cells = split_into_cells(AREA, 6)
    result = auction_cells(cells, _fleet(100, 30), min_battery_pct=20, cell_work_m=_work(cells))
    assert len(result.cells_for("sim-2")) < len(result.cells_for("sim-1"))


def test_drones_at_or_below_the_minimum_battery_do_not_bid():
    cells = split_into_cells(AREA, 4)
    result = auction_cells(cells, _fleet(90, 20), min_battery_pct=20, cell_work_m=_work(cells))
    assert result.cells_for("sim-2") == []
    assert "sim-2" in result.excluded
    assert len(result.cells_for("sim-1")) == 4


def test_no_eligible_drones_means_no_awards():
    cells = split_into_cells(AREA, 4)
    result = auction_cells(cells, _fleet(10, 5), min_battery_pct=20, cell_work_m=_work(cells))
    assert result.awards == []
    assert set(result.excluded) == {"sim-1", "sim-2"}


def test_lowest_id_is_coordinator_everyone_else_scouts():
    roles = assign_roles(["sim-3", "sim-1", "sim-2"])
    assert roles == {"sim-1": VehicleRole.COORDINATOR, "sim-2": VehicleRole.SCOUT, "sim-3": VehicleRole.SCOUT}
