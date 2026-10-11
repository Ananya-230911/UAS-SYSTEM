"""Geofence geometry for the fleet-agent safety layer
(docs/adr/0016-fleet-agent-phase-2-models-and-safety.md).

point_in_polygon() is the same ray-casting algorithm as
services/api/app/geofence.py's function of the same name --
deliberately duplicated rather than imported across services, the same
"services stay independent, small pieces get re-implemented rather
than shared" pattern services/telemetry-gateway/gateway/commands.py's
MODE_NAMES and simulation/sitl/sim.py's MODE_CODES already use (see
docs/adr/0006-internal-messaging.md).

segment_crosses_fence_boundary() is new: point-in-polygon alone only
checks waypoints themselves, not whether the straight line BETWEEN two
inside waypoints clips outside a concave fence -- Phase 2 of
docs/IMPLEMENTATION_PLAN.md explicitly asks for "every route segment"
to be checked, not just every waypoint.
"""
import math

Point = dict  # {"lat": float, "lon": float}

EARTH_RADIUS_M = 6371000.0


def point_in_polygon(lat: float, lon: float, polygon: list[Point]) -> bool:
    """Standard ray-casting point-in-polygon test, treating (lon, lat)
    as plane coordinates -- an approximation that ignores Earth's
    curvature, fine at geofence scale (meters to a few km). A point
    exactly on the boundary is treated as inside, matching
    services/api/app/geofence.py's identical fail-open-on-the-line
    choice."""
    if len(polygon) < 3:
        return False

    inside = False
    n = len(polygon)
    x, y = lon, lat
    for i in range(n):
        x1, y1 = polygon[i]["lon"], polygon[i]["lat"]
        x2, y2 = polygon[(i + 1) % n]["lon"], polygon[(i + 1) % n]["lat"]
        if y1 == y2 == y and min(x1, x2) <= x <= max(x1, x2):
            return True
        if (y1 > y) != (y2 > y):
            x_intersect = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x_intersect == x:
                return True
            if x_intersect > x:
                inside = not inside
    return inside


def _orientation(a: tuple, b: tuple, c: tuple) -> int:
    val = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if val == 0:
        return 0
    return 1 if val > 0 else 2


def _on_segment(a: tuple, b: tuple, c: tuple) -> bool:
    return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])


def _segments_intersect(p1: tuple, p2: tuple, p3: tuple, p4: tuple) -> bool:
    """Standard orientation-based segment-segment intersection test,
    including the collinear/touching edge cases."""
    o1, o2 = _orientation(p1, p2, p3), _orientation(p1, p2, p4)
    o3, o4 = _orientation(p3, p4, p1), _orientation(p3, p4, p2)

    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and _on_segment(p1, p2, p3):
        return True
    if o2 == 0 and _on_segment(p1, p2, p4):
        return True
    if o3 == 0 and _on_segment(p3, p4, p1):
        return True
    if o4 == 0 and _on_segment(p3, p4, p2):
        return True
    return False


def segment_crosses_fence_boundary(p1: Point, p2: Point, polygon: list[Point]) -> bool:
    """True if the straight segment p1->p2 crosses any edge of
    polygon -- catches a route that clips outside a concave fence even
    when both endpoints are themselves inside it."""
    a = (p1["lon"], p1["lat"])
    b = (p2["lon"], p2["lat"])
    n = len(polygon)
    for i in range(n):
        c = (polygon[i]["lon"], polygon[i]["lat"])
        d = (polygon[(i + 1) % n]["lon"], polygon[(i + 1) % n]["lat"])
        if _segments_intersect(a, b, c, d):
            return True
    return False


def horizontal_distance_m(a: Point, b: Point) -> float:
    """Flat-earth approximation, the same simplification
    simulation/sitl/sim.py's own _latlon_offset_m() already makes at
    this scale."""
    lat1, lon1 = math.radians(a["lat"]), math.radians(a["lon"])
    lat2, lon2 = math.radians(b["lat"]), math.radians(b["lon"])
    north_m = (lat2 - lat1) * EARTH_RADIUS_M
    east_m = (lon2 - lon1) * EARTH_RADIUS_M * math.cos((lat1 + lat2) / 2)
    return math.hypot(north_m, east_m)
