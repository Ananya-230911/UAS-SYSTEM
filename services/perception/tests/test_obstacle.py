from app.obstacle import OBSTACLE_RANGE_THRESHOLD_M, find_obstacles


def _reading(bearing, range_m):
    return {"bearing_deg": bearing, "range_m": range_m}


def test_no_close_readings_means_no_obstacles():
    scan = [_reading(i * 10, 15.0) for i in range(36)]
    assert find_obstacles(scan) == []


def test_a_single_close_reading_is_one_obstacle():
    scan = [_reading(i * 10, 15.0) for i in range(36)]
    scan[5]["range_m"] = 3.0
    obstacles = find_obstacles(scan)
    assert len(obstacles) == 1
    assert obstacles[0]["range_m"] == 3.0


def test_adjacent_close_readings_merge_into_one_obstacle():
    scan = [_reading(i * 10, 15.0) for i in range(36)]
    scan[5]["range_m"] = 4.0
    scan[6]["range_m"] = 3.0  # within CLUSTER_GAP_DEG of beam 5 -- same cluster
    scan[7]["range_m"] = 3.5
    obstacles = find_obstacles(scan)
    assert len(obstacles) == 1
    assert obstacles[0]["range_m"] == 3.0  # the closest reading in the cluster


def test_far_apart_close_readings_are_separate_obstacles():
    scan = [_reading(i * 10, 15.0) for i in range(36)]
    scan[0]["range_m"] = 3.0
    scan[18]["range_m"] = 4.0  # far enough away (180 degrees) to be a distinct obstacle
    obstacles = find_obstacles(scan)
    assert len(obstacles) == 2


def test_threshold_boundary_is_exclusive():
    scan = [_reading(i * 10, 15.0) for i in range(36)]
    scan[0]["range_m"] = OBSTACLE_RANGE_THRESHOLD_M  # exactly at threshold -- not "within"
    assert find_obstacles(scan) == []
