from app.lidar import MAX_RANGE_M, MIN_CLEAR_RANGE_M, NUM_BEAMS, current_scan


def test_scan_shape_and_bounds():
    scan = current_scan("sim-1", tick=1000)
    assert len(scan) == NUM_BEAMS
    bearings = [r["bearing_deg"] for r in scan]
    assert bearings == sorted(bearings)  # ordered by bearing, obstacle.py depends on this
    for r in scan:
        assert 0.0 <= r["range_m"] <= MAX_RANGE_M


def test_same_vehicle_and_tick_is_deterministic():
    a = current_scan("sim-1", tick=1000)
    b = current_scan("sim-1", tick=1000)
    assert a == b


def test_different_ticks_can_differ():
    a = current_scan("sim-1", tick=1000)
    b = current_scan("sim-1", tick=1001)
    assert a != b


def test_different_vehicles_can_differ_at_the_same_tick():
    a = current_scan("sim-1", tick=1000)
    b = current_scan("sim-2", tick=1000)
    assert a != b


def test_some_ticks_produce_a_close_obstacle_cluster():
    # ~30% of ticks inject a close cluster -- sweep enough ticks that at
    # least one should, without hardcoding which tick (keeps this
    # robust to the exact LCG constants rather than overfit to one seed).
    found_close = any(
        any(r["range_m"] < MIN_CLEAR_RANGE_M for r in current_scan("sim-1", tick=t))
        for t in range(1000, 1050)
    )
    assert found_close
