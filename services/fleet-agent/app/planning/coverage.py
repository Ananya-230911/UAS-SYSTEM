"""Area coverage: split a search area into cells, then a lawnmower
(back-and-forth) path per cell (docs/IMPLEMENTATION_PLAN.md Phase 3).

Deliberately simple geometry: cells are a rectangular grid over the
area's bounding box, and a sweep line is a constant-latitude line. Same
flat-earth approximation the rest of this project already uses at
sub-kilometre scale (app/safety/geofence.py, simulation/sitl/sim.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.models import GeofencePoint, SearchArea, Waypoint
from app.safety.geofence import EARTH_RADIUS_M, point_in_polygon

M_PER_DEG_LAT = math.pi / 180.0 * EARTH_RADIUS_M


def m_per_deg_lon(lat: float) -> float:
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


@dataclass(frozen=True)
class Cell:
    cell_id: str
    south: float
    west: float
    north: float
    east: float

    @property
    def centroid(self) -> tuple[float, float]:
        return ((self.south + self.north) / 2, (self.west + self.east) / 2)

    @property
    def polygon(self) -> list[GeofencePoint]:
        return [
            GeofencePoint(lat=self.south, lon=self.west),
            GeofencePoint(lat=self.south, lon=self.east),
            GeofencePoint(lat=self.north, lon=self.east),
            GeofencePoint(lat=self.north, lon=self.west),
        ]


def camera_footprint_spacing_m(altitude_m: float, fov_deg: float = 60.0, overlap: float = 0.2) -> float:
    """Distance between neighbouring sweep lines so the camera's ground
    footprint covers everything with `overlap` to spare. Footprint
    width for a downward camera = 2 * altitude * tan(fov / 2). The
    60 degree FOV is a demonstration default, not a specific camera."""
    if altitude_m <= 0:
        raise ValueError("altitude_m must be positive to have a camera footprint")
    if not 0 <= overlap < 1:
        raise ValueError("overlap must be in [0, 1)")
    footprint = 2 * altitude_m * math.tan(math.radians(fov_deg) / 2)
    return footprint * (1 - overlap)


def split_into_cells(area: SearchArea, min_cells: int) -> list[Cell]:
    """A rows x cols grid over the area's bounding box with at least
    min_cells cells, keeping only cells whose centre is inside the area
    polygon (so a non-rectangular area doesn't get cells that are
    mostly outside it). For a rectangular area every cell is kept and
    the cells tile it exactly, with no gaps or overlaps."""
    if min_cells < 1:
        raise ValueError("min_cells must be at least 1")
    polygon = [p.model_dump() for p in area.points]
    south = min(p["lat"] for p in polygon)
    north = max(p["lat"] for p in polygon)
    west = min(p["lon"] for p in polygon)
    east = max(p["lon"] for p in polygon)

    cols = math.ceil(math.sqrt(min_cells))
    rows = math.ceil(min_cells / cols)
    dlat = (north - south) / rows
    dlon = (east - west) / cols

    cells = []
    for r in range(rows):
        for c in range(cols):
            cell = Cell(
                cell_id=f"r{r}c{c}",
                south=south + r * dlat,
                west=west + c * dlon,
                north=south + (r + 1) * dlat,
                east=west + (c + 1) * dlon,
            )
            lat, lon = cell.centroid
            if point_in_polygon(lat, lon, polygon):
                cells.append(cell)
    return cells


def lawnmower_path(cell: Cell, spacing_m: float, altitude_m: float) -> list[Waypoint]:
    """Back-and-forth sweep lines across the cell. Lines are evenly
    spread so every point in the cell is within spacing_m / 2 of a line
    (north-south), and each line stops spacing_m / 2 short of the
    cell's east/west edges -- the camera footprint still reaches the
    edge, and the path never sits exactly on the cell boundary (which,
    for a cell on the geofence edge, would be the fence itself)."""
    if spacing_m <= 0:
        raise ValueError("spacing_m must be positive")
    center_lat = (cell.south + cell.north) / 2
    lon_scale = m_per_deg_lon(center_lat)
    height_m = (cell.north - cell.south) * M_PER_DEG_LAT
    width_m = (cell.east - cell.west) * lon_scale

    n_lines = max(1, math.ceil(height_m / spacing_m))
    line_gap_m = height_m / n_lines
    inset_m = min(spacing_m / 2, width_m / 2)
    west_i = cell.west + inset_m / lon_scale
    east_i = cell.east - inset_m / lon_scale

    waypoints: list[Waypoint] = []
    for i in range(n_lines):
        lat = cell.south + (i + 0.5) * line_gap_m / M_PER_DEG_LAT
        ends = [(lat, west_i), (lat, east_i)]
        if i % 2 == 1:
            ends.reverse()
        for lat_, lon_ in ends:
            wp = Waypoint(lat=lat_, lon=lon_, alt_m=altitude_m)
            if not waypoints or (waypoints[-1].lat, waypoints[-1].lon) != (wp.lat, wp.lon):
                waypoints.append(wp)
    return waypoints


def path_length_m(waypoints: list[Waypoint]) -> float:
    total = 0.0
    for a, b in zip(waypoints, waypoints[1:]):
        north = (b.lat - a.lat) * M_PER_DEG_LAT
        east = (b.lon - a.lon) * m_per_deg_lon((a.lat + b.lat) / 2)
        total += math.hypot(north, east)
    return total
