import sys
import types

import pytest

import app.camera as camera_module
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


def test_use_webcam_defaults_off():
    # Regression guard: without PERCEPTION_USE_WEBCAM=1 set, the module
    # must default to the simulated sample-image path -- CI/tests have
    # no camera attached and must stay deterministic.
    assert camera_module.USE_WEBCAM is False


def test_webcam_frame_used_when_enabled(monkeypatch, tmp_path):
    # Fakes cv2 entirely (sys.modules injection) so this test needs
    # neither a real webcam nor opencv actually installed -- it only
    # proves camera.py's own branching/wiring is correct.
    fake_cv2 = types.SimpleNamespace(imwrite=lambda path, frame: True)
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    monkeypatch.setattr(camera_module, "USE_WEBCAM", True)
    monkeypatch.setattr(camera_module, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(camera_module, "WEBCAM_FRAME_PATH", tmp_path / "webcam_frame.jpg")

    class FakeCam:
        def read(self):
            return True, "fake-frame-data"

    monkeypatch.setattr(camera_module, "_get_webcam", lambda: FakeCam())

    path = camera_module.current_frame_path("sim-1")
    assert path == tmp_path / "webcam_frame.jpg"


def test_webcam_read_failure_raises(monkeypatch):
    monkeypatch.setattr(camera_module, "USE_WEBCAM", True)

    class FakeCam:
        def read(self):
            return False, None

    monkeypatch.setattr(camera_module, "_get_webcam", lambda: FakeCam())

    with pytest.raises(RuntimeError):
        camera_module.current_frame_path("sim-1")
