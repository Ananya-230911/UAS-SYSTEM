from app.geofence import point_in_polygon, waypoints_outside_fence

# A simple 1-degree square, roughly centered on the simulator's Stanford
# default home (see simulation/sitl/sim.py) -- easy to reason about by
# hand, not meant to represent a real-world fence size.
SQUARE = [
    {"lat": 37.0, "lon": -123.0},
    {"lat": 37.0, "lon": -122.0},
    {"lat": 38.0, "lon": -122.0},
    {"lat": 38.0, "lon": -123.0},
]


def test_point_clearly_inside_is_inside():
    assert point_in_polygon(37.5, -122.5, SQUARE) is True


def test_point_clearly_outside_is_outside():
    assert point_in_polygon(40.0, -122.5, SQUARE) is False
    assert point_in_polygon(37.5, -100.0, SQUARE) is False


def test_point_on_boundary_counts_as_inside():
    # Fail-open on the line itself -- see the docstring in geofence.py for
    # why: the failsafe only needs to care about being clearly outside.
    assert point_in_polygon(37.0, -122.5, SQUARE) is True  # on the bottom edge
    assert point_in_polygon(37.5, -123.0, SQUARE) is True  # on the left edge


def test_degenerate_polygon_contains_nothing():
    assert point_in_polygon(37.5, -122.5, []) is False
    assert point_in_polygon(37.5, -122.5, [{"lat": 37.0, "lon": -122.0}]) is False
    assert (
        point_in_polygon(
            37.5, -122.5, [{"lat": 37.0, "lon": -122.0}, {"lat": 38.0, "lon": -122.0}]
        )
        is False
    )


def test_waypoints_outside_fence_reports_indices():
    waypoints = [
        {"lat": 37.5, "lon": -122.5, "alt_m": 20},  # inside
        {"lat": 50.0, "lon": -122.5, "alt_m": 20},  # outside
        {"lat": 37.6, "lon": -122.6, "alt_m": 20},  # inside
        {"lat": 37.5, "lon": 0.0, "alt_m": 20},  # outside
    ]
    assert waypoints_outside_fence(waypoints, SQUARE) == [1, 3]


def test_waypoints_all_inside_reports_nothing():
    waypoints = [{"lat": 37.5, "lon": -122.5, "alt_m": 20}]
    assert waypoints_outside_fence(waypoints, SQUARE) == []
