import cv2
import numpy as np
import pytest

from poop_detector.camera import Camera
from poop_detector.config import Config
from poop_detector.detection import Detector
from poop_detector.events import Store
from poop_detector.notifications import MacNotifier
from poop_detector.roi import ROI
from poop_detector.state_machine import Machine


def frames():
    baseline = np.full((200, 200, 3), 200, np.uint8)
    changed = baseline.copy()
    cv2.rectangle(changed, (80, 80), (100, 100), (30, 30, 30), -1)
    return baseline, changed


def test_detection_small_object_and_illumination():
    baseline, changed = frames()
    detector = Detector(baseline)
    assert detector.analyze(baseline).kind == "empty"
    assert detector.analyze(changed).kind == "candidate"
    assert detector.analyze(np.full_like(baseline, 150)).kind == "unstable"


def test_occlusion_is_not_feces():
    baseline, _ = frames()
    changed = baseline.copy()
    changed[30:170, 30:170] = 10
    assert Detector(baseline).analyze(changed).kind == "occupied"


def test_resolution_mismatch_rejected():
    baseline, _ = frames()
    with pytest.raises(ValueError):
        Detector(baseline).analyze(baseline[:100])


def test_roi_roundtrip_and_scaling(tmp_path):
    roi = ROI.from_pixels((20, 40, 100, 80), 200, 200)
    path = tmp_path / "roi.json"
    roi.save(path)
    assert ROI.load(path).pixels(400, 400) == (40, 80, 200, 160)
    with pytest.raises(ValueError):
        ROI.from_pixels((0, 0, 0, 20), 200, 200)


def test_machine_persistence_duplicates_and_clear():
    machine = Machine(verify_seconds=2, clear_seconds=2)
    assert not machine.update("occupied", 0)
    assert machine.state == "OCCUPIED"
    assert not machine.update("candidate", 1)
    assert machine.update("candidate", 3)
    assert machine.state == "SUSPECTED"
    assert not machine.update("candidate", 50)
    machine.acknowledge()
    assert not machine.update("candidate", 60)
    machine.update("empty", 61)
    machine.update("empty", 63)
    assert machine.state == "EMPTY"
    assert not machine.update("candidate", 64)
    assert machine.update("candidate", 66)


def test_interruption_resets_verification():
    machine = Machine(verify_seconds=2)
    machine.update("candidate", 0)
    machine.update("unstable", 1)
    assert not machine.update("candidate", 3)
    assert machine.update("candidate", 5)


def test_events_feedback_metrics_and_image(tmp_path):
    baseline, _ = frames()
    store = Store(tmp_path)
    event = store.record(baseline, 0.4, "2026-11-01T00:00:00+00:00", 10.0)
    assert (tmp_path / store.list()[0]["image"]).exists()
    assert store.metrics()["precision"] is None
    store.feedback(event, True)
    store.missed("2026-11-01T01:00:00+00:00")
    assert store.metrics()["precision"] == 1
    assert store.metrics()["recall"] == 0.5
    assert store.metrics()["missed"] == 1
    with pytest.raises(ValueError):
        store.feedback("unknown", False)


def test_camera_reconnects_and_releases():
    baseline, _ = frames()

    class Capture:
        def __init__(self, succeeds):
            self.succeeds = succeeds
            self.released = False

        def isOpened(self):
            return True

        def read(self):
            return self.succeeds, baseline if self.succeeds else None

        def release(self):
            self.released = True

    captures = [Capture(False), Capture(True)]
    opened = []

    def factory(*args):
        capture = captures[len(opened)]
        opened.append(capture)
        return capture

    camera = Camera("rtsp://user:secret@localhost/live", factory=factory, retry_seconds=0)
    with camera:
        assert camera.read() is None
        assert camera.read().shape == baseline.shape
    assert all(c.released for c in captures)


def test_notification_disabled_and_safe_arguments():
    calls = []
    notifier = MacNotifier(enabled=False, runner=lambda *a, **kw: calls.append((a, kw)))
    assert not notifier.send()
    notifier.enabled = True
    assert notifier.send()
    assert calls[0][1]["shell"] is False


def test_config_rejects_invalid_values(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("fps: 0\n")
    with pytest.raises(ValueError):
        Config.load(path)


def test_cli_help_and_missing_url(tmp_path, monkeypatch, capsys):
    from poop_detector.main import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POOP_RTSP_URL", raising=False)
    assert main(["check-camera"]) == 2
    assert "RTSP" in capsys.readouterr().err
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0


@pytest.mark.parametrize("codex_notifications", [False, True])
def test_offline_pipeline_records_one_event(tmp_path, codex_notifications):
    from poop_detector.main import monitor

    baseline, changed = frames()

    class Frames:
        generation = 1

        def read(self):
            return changed.copy()

    class Notifier:
        def send(self):
            assert not codex_notifications
            return True

    config = Config(data_dir=str(tmp_path), fps=100, verify_seconds=0.01)
    monitor(
        Frames(),
        ROI(0, 0, 1, 1),
        baseline,
        config,
        Notifier(),
        max_frames=8,
        codex_notifications=codex_notifications,
    )
    events = Store(tmp_path).list()
    assert len(events) == 1
    assert (events[0]["notified_at"] is None) == codex_notifications


def test_latest_camera_discards_old_frames():
    import threading

    from poop_detector.camera import LatestCamera

    done = threading.Event()
    finish = threading.Event()
    baseline, _ = frames()

    class Source:
        generation = 1
        timeout_ms = 100

        def __enter__(self):
            return self

        def __exit__(self, *args):
            finish.set()

        def read(self):
            if done.is_set():
                finish.wait(1)
                return None
            return baseline

    source = Source()
    with LatestCamera(source) as camera:
        assert camera.wait_ready(1)
        assert camera.read() is not None
        done.set()
    assert finish.is_set()


def test_baseline_and_roi_cli_do_not_overwrite(tmp_path, monkeypatch, capsys):
    from poop_detector.main import main

    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {tmp_path}\n")
    (tmp_path / "roi.json").write_text("{}")
    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://private:secret@localhost/live")
    assert main(["--config", str(config), "setup-roi"]) == 2
    assert "secret" not in capsys.readouterr().err
    assert (tmp_path / "roi.json").read_text() == "{}"


def test_latest_camera_overwrites_pending_frame():
    import threading

    from poop_detector.camera import LatestCamera

    ready = threading.Event()
    release = threading.Event()

    class Source:
        generation = 1
        timeout_ms = 100
        count = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            self.count += 1
            if self.count > 3:
                ready.set()
                release.wait(1)
                return None
            return np.full((2, 2, 3), self.count, np.uint8)

    with LatestCamera(Source()) as camera:
        try:
            assert ready.wait(1)
            assert int(camera.read()[0, 0, 0]) == 3
            assert camera.read() is None
        finally:
            release.set()


def test_baseline_rejects_changed_roi(tmp_path):
    from poop_detector.main import load_baseline, save_baseline

    baseline, _ = frames()
    roi = ROI(0, 0, 0.5, 0.5)
    path = tmp_path / "empty.png"
    save_baseline(path, baseline, roi)
    assert load_baseline(path, roi).shape == baseline.shape
    with pytest.raises(ValueError):
        load_baseline(path, ROI(0.5, 0.5, 0.5, 0.5))


def test_real_decoder_to_event_pipeline(tmp_path):
    from poop_detector.main import monitor

    baseline, changed = frames()
    path = tmp_path / "synthetic.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (200, 200))
    assert writer.isOpened()
    for _ in range(8):
        writer.write(changed)
    writer.release()

    class Silent:
        def send(self):
            return False

    config = Config(data_dir=str(tmp_path / "output"), fps=100, verify_seconds=0.01)
    with Camera(str(path)) as camera:
        assert monitor(camera, ROI(0, 0, 1, 1), baseline, config, Silent(), max_frames=8) == 8
    assert len(Store(config.data_dir).list()) == 1


def test_notification_failure_does_not_crash():
    import subprocess

    def fails(*args, **kwargs):
        raise subprocess.TimeoutExpired("osascript", 5)

    assert not MacNotifier(runner=fails).send()


def test_metrics_delay_uses_ground_truth(tmp_path):
    baseline, _ = frames()
    store = Store(tmp_path)
    event = store.record(baseline, 0.2, "2026-11-01T00:00:00+00:00", 1)
    store.feedback(event, True, "2026-11-01T00:00:00+00:00", "2026-11-01T00:00:02+00:00")
    store.update(event, notified_at="2026-11-01T00:00:05+00:00")
    assert store.metrics()["start_delay_seconds"] == [5]
    assert store.metrics()["end_delay_seconds"] == [3]


def test_restart_after_cleaning_rearms(tmp_path):
    from poop_detector.main import monitor

    baseline, changed = frames()

    class Source:
        generation = 1

        def __init__(self, frame):
            self.frame = frame

        def read(self):
            return self.frame

    class Silent:
        def send(self):
            return False

    config = Config(data_dir=str(tmp_path), fps=100, verify_seconds=0.01, clear_seconds=0.01)
    for frame in [changed, baseline, changed]:
        monitor(Source(frame), ROI(0, 0, 1, 1), baseline, config, Silent(), max_frames=5)
    assert len(Store(tmp_path).list()) == 2


def test_waiting_for_new_frame_keeps_verification(tmp_path):
    from poop_detector.main import monitor

    baseline, changed = frames()

    class Source:
        generation = 1
        healthy = True
        count = 0

        def read(self):
            self.count += 1
            return changed if self.count % 2 else None

    class Silent:
        def send(self):
            return False

    config = Config(data_dir=str(tmp_path), fps=100, verify_seconds=0.03)
    monitor(Source(), ROI(0, 0, 1, 1), baseline, config, Silent(), max_frames=6)
    assert len(Store(tmp_path).list()) == 1
