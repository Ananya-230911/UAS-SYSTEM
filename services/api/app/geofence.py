"""Geofence geometry (docs/adr/0012-geofencing-failsafe.md).

A geofence is a polygon of {lat, lon} points -- the same shape the UI
already collects for mission waypoints (docs/adr/0008-mission-protocol.md
established "click the map to add points" as the interaction; a fence is
drawn the same way, just closed into a boundary instead of a route).

This is pure geometry with no framework/DB dependency, so it's unit
testable without spinning up the API (see tests/test_geofence.py).
"""

Point = dict  # {"lat": float, "lon": float}


def point_in_polygon(lat: float, lon: float, polygon: list[Point]) -> bool:
    """Standard ray-casting point-in-polygon test, treating (lon, lat) as
    plane coordinates. This is an approximation (it ignores Earth's
    curvature), but at the scale of a geofence -- meters to a few
    kilometers -- the distortion is negligible, and it's exact enough for
    the same reason equirectangular projections are fine for small areas.
    A vehicle exactly on the boundary is treated as inside (fail-open on
    the fence line itself; the failsafe only cares about being clearly
    outside)."""
    if len(polygon) < 3:
        return False  # a degenerate "polygon" contains nothing

    inside = False
    n = len(polygon)
    x, y = lon, lat
    for i in range(n):
        x1, y1 = polygon[i]["lon"], polygon[i]["lat"]
        x2, y2 = polygon[(i + 1) % n]["lon"], polygon[(i + 1) % n]["lat"]
        if y1 == y2 == y and min(x1, x2) <= x <= max(x1, x2):
            return True  # on a horizontal edge -- treat as inside
        if (y1 > y) != (y2 > y):
            x_intersect = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x_intersect == x:
                return True  # exactly on the boundary
            if x_intersect > x:
                inside = not inside
    return inside


def waypoints_outside_fence(waypoints: list[Point], polygon: list[Point]) -> list[int]:
    """Indices (0-based) of every waypoint that falls outside polygon.
    Used to reject a mission upload before it ever reaches the vehicle --
    see create_mission() in app/main.py."""
    return [i for i, wp in enumerate(waypoints) if not point_in_polygon(wp["lat"], wp["lon"], polygon)]
