import math

import pytest

from app.core.models import GeofencePoint, SearchArea
from app.planning.coverage import (
    M_PER_DEG_LAT,
    camera_footprint_spacing_m,
    lawnmower_path,
    m_per_deg_lon,
    split_into_cells,
)

RECT = SearchArea(
    points=[
        GeofencePoint(lat=37.4250, lon=-122.1720),
        GeofencePoint(lat=37.4250, lon=-122.1650),
        GeofencePoint(lat=37.4310, lon=-122.1650),
        GeofencePoint(lat=37.4310, lon=-122.1720),
    ]
)


def test_footprint_spacing_matches_the_formula():
    # 2 * 20 * tan(30 deg) * (1 - 0.2)
    assert camera_footprint_spacing_m(20.0) == pytest.approx(2 * 20 * math.tan(math.radians(30)) * 0.8)


def test_footprint_spacing_rejects_nonsense():
    with pytest.raises(ValueError):
        camera_footprint_spacing_m(0)
    with pytest.raises(ValueError):
        camera_footprint_spacing_m(20, overlap=1.0)


def test_rectangular_area_is_tiled_exactly_with_no_gaps_or_overlaps():
    cells = split_into_cells(RECT, 6)
    assert len(cells) == 6
    total_area = sum((c.north - c.south) * (c.east - c.west) for c in cells)
    assert total_area == pytest.approx((37.4310 - 37.4250) * (-122.1650 - -122.1720))
    for i, a in enumerate(cells):
        for b in cells[i + 1:]:
            overlap_lat = min(a.north, b.north) - max(a.south, b.south)
            overlap_lon = min(a.east, b.east) - max(a.west, b.west)
            assert not (overlap_lat > 1e-12 and overlap_lon > 1e-12), f"{a.cell_id} overlaps {b.cell_id}"


def test_cells_outside_a_non_rectangular_area_are_dropped():
    triangle = SearchArea(
        points=[GeofencePoint(lat=0, lon=0), GeofencePoint(lat=0, lon=1), GeofencePoint(lat=1, lon=0)]
    )
    cells = split_into_cells(triangle, 9)
    # The far corner cell (centre around lat 0.83, lon 0.83) is outside the triangle.
    assert "r2c2" not in {c.cell_id for c in cells}
    assert 0 < len(cells) < 9


def test_lawnmower_leaves_no_gaps_in_its_cell():
    spacing = 18.5
    cell = split_into_cells(RECT, 6)[0]
    path = lawnmower_path(cell, spacing, 20.0)
    line_lats = sorted({wp.lat for wp in path})
    lon_scale = m_per_deg_lon((cell.south + cell.north) / 2)
    west_reach = min(wp.lon for wp in path) - (spacing / 2) / lon_scale
    east_reach = max(wp.lon for wp in path) + (spacing / 2) / lon_scale

    # Every point of a fine grid over the cell is within half a spacing
    # of some sweep line (north-south), and inside the swept east-west
    # span once the camera's half-footprint is added.
    for i in range(21):
        lat = cell.south + (cell.north - cell.south) * i / 20
        nearest_m = min(abs(lat - line) for line in line_lats) * M_PER_DEG_LAT
        assert nearest_m <= spacing / 2 + 1e-6
    assert west_reach <= cell.west + 1e-12
    assert east_reach >= cell.east - 1e-12


def test_lawnmower_alternates_direction_and_stays_inside_the_cell():
    cell = split_into_cells(RECT, 6)[0]
    path = lawnmower_path(cell, 18.5, 20.0)
    assert path[0].lon < path[1].lon  # first line west -> east
    assert path[2].lon > path[3].lon  # second line east -> west
    for wp in path:
        assert cell.south < wp.lat < cell.north
        assert cell.west < wp.lon < cell.east
        assert wp.alt_m == 20.0


def test_lawnmower_rejects_non_positive_spacing():
    with pytest.raises(ValueError):
        lawnmower_path(split_into_cells(RECT, 1)[0], 0, 20.0)
