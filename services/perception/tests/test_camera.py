from app.camera import available_frames, current_frame_path


def test_sample_frames_exist():
    # Regression guard: the service is useless without its bundled demo
    # frames -- catch a missing/empty sample_frames dir immediately
    # rather than as a confusing downstream detection failure.
    assert len(available_frames()) >= 1


def test_current_frame_is_one_of_the_available_frames():
    frames = available_frames()
    path = current_frame_path("sim-1", now=1_000_000.0)
    assert path in frames


def test_same_tick_returns_same_frame():
    a = current_frame_path("sim-1", now=1_000_000.0)
    b = current_frame_path("sim-1", now=1_000_000.4)  # same 5s bucket
    assert a == b


def test_different_vehicles_can_get_different_frames():
    # Not guaranteed for every possible id pair, but true for these two
    # specific ids at this specific instant -- pins the offset behavior.
    a = current_frame_path("sim-1", now=1_000_000.0)
    b = current_frame_path("sim-2", now=1_000_000.0)
    frames = available_frames()
    if len(frames) > 1:
        assert a != b
