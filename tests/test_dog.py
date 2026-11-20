"""犬の検出、領域との重なり、入退場の揺れを確認する。"""

import numpy as np
import pytest

from poop_detector.detection.roi import ROI, MaskedROI


def test_model_decodes_dog_only_and_clips_box():
    from poop_detector.features.dog import DogDetector

    class Net:
        def setInput(self, blob):
            assert blob.shape == (1, 3, 640, 640)

        def forward(self):
            output = np.zeros((1, 8400, 85), dtype=np.float32)
            output[0, 0, :4] = [24, 24, np.log(48), np.log(32)]
            output[0, 0, 4] = 0.9
            output[0, 0, 5 + 16] = 0.9
            output[0, 1, 4] = 0.9
            output[0, 1, 5 + 15] = 0.9
            return output

    detector = DogDetector(None, net=Net())
    assert detector.predict(np.zeros((100, 200, 3), np.uint8)) == [(0, 20, 120, 99)]


def test_observer_entrance_exit_and_roi_occlusion():
    from poop_detector.features.dog import DogObserver

    boxes = [(20, 20, 50, 50)]

    class Detector:
        def predict(self, frame):
            return boxes

    observer = DogObserver(Detector(), fps=1, exit_seconds=2)
    frame = np.zeros((100, 100, 3), np.uint8)
    roi = ROI(0.1, 0.1, 0.5, 0.5)
    state = observer.update(frame, roi, 0)
    assert state.transition == "dog_enter"
    assert state.on_roi
    assert observer.update(frame, roi, 0.5).transition is None
    boxes.clear()
    assert observer.update(frame, roi, 1).present
    assert observer.update(frame, roi, 2).transition == "dog_exit"
    assert not observer.update(frame, roi, 3).on_roi


def test_polygon_outside_bbox_is_not_occlusion():
    from poop_detector.features.dog import overlaps_roi

    roi = MaskedROI.from_pixels(
        [(50, 10), (90, 30), (90, 70), (50, 90), (10, 70), (10, 30)], 101, 101
    )
    assert not overlaps_roi((10, 10, 15, 15), roi, (101, 101, 3))
    assert overlaps_roi((45, 45, 60, 60), roi, (101, 101, 3))


@pytest.mark.parametrize("interruption", [None, "error", "reset"])
def test_observer_measures_completed_tray_stay(interruption):
    from poop_detector.features.dog import DogObserver

    class Detector:
        inside = False
        failed = False

        def predict(self, frame):
            if self.failed:
                raise RuntimeError("テスト用の推論失敗")
            return [(20, 20, 50, 50)] if self.inside else []

    detector = Detector()
    observer = DogObserver(detector, fps=1, exit_seconds=2)
    frame = np.zeros((100, 100, 3), np.uint8)
    roi = ROI(0, 0, 1, 1)
    assert not observer.update(frame, roi, -1).on_roi
    detector.inside = True
    assert observer.update(frame, roi, 0).transition == "dog_enter"
    for now in range(1, 5):
        assert observer.update(frame, roi, now).on_roi
    if interruption == "reset":
        observer.reset()
        assert observer.update(frame, roi, 4).on_roi
    detector.inside = False
    detector.failed = interruption == "error"
    observer.update(frame, roi, 5)
    detector.failed = False
    exit_state = observer.update(frame, roi, 6)
    assert exit_state.tray_stay_seconds == (4 if interruption is None else None)


def test_inference_failure_is_held_until_recovery():
    from poop_detector.features.dog import DogObserver

    class Detector:
        def predict(self, frame):
            raise RuntimeError("テスト用の推論失敗")

    observer = DogObserver(Detector())
    state = observer.update(np.zeros((100, 100, 3), np.uint8), ROI(0, 0, 1, 1), 0)
    assert state.error


def test_missing_model_does_not_download_silently(tmp_path):
    from poop_detector.features.dog import DogDetector

    with pytest.raises(FileNotFoundError):
        DogDetector(tmp_path)


def test_visible_dog_outside_roi_enters_when_moving_inside():
    from poop_detector.features.dog import DogObserver

    boxes = [(70, 70, 90, 90)]

    class Detector:
        def predict(self, frame):
            return boxes.copy()

    observer = DogObserver(Detector())
    frame = np.zeros((100, 100, 3), np.uint8)
    roi = ROI(0, 0, 0.5, 0.5)
    first = observer.update(frame, roi, 0)
    assert first.present
    assert not first.on_roi
    assert first.transition is None
    boxes[:] = [(20, 20, 40, 40)]
    assert observer.update(frame, roi, 1).transition == "dog_enter"


def test_monitor_does_not_notify_while_dog_occludes_roi(tmp_path):
    from poop_detector.config import Config
    from poop_detector.features.dog import DogState
    from poop_detector.features.monitoring import monitor
    from poop_detector.records.events import Store

    base = np.full((200, 200, 3), 200, np.uint8)
    changed = base.copy()
    changed[80:100, 80:100] = 20

    class Camera:
        generation = 1
        healthy = True

        def read(self):
            return changed

    class Observer:
        def update(self, *args):
            return DogState(True, True, None, [], 1)

    class Notifier:
        def send(self, *, clip_id=None, detected_at=None):
            raise AssertionError("遮蔽中に通知しました")

    monitor(
        Camera(),
        ROI(0, 0, 1, 1),
        base,
        Config(data_dir=str(tmp_path), fps=100, verify_seconds=0.01),
        Notifier(),
        max_frames=5,
        dog_observer=Observer(),
    )
    assert Store(tmp_path).list() == []


def test_monitor_collects_entry_exit_and_detection(tmp_path):
    from poop_detector.config import Config
    from poop_detector.features.dog import DogState
    from poop_detector.features.monitoring import monitor
    from poop_detector.records.events import Store

    base = np.full((200, 200, 3), 200, np.uint8)
    changed = base.copy()
    changed[80:100, 80:100] = 20

    class Camera:
        generation = 1
        healthy = True

        def read(self):
            return changed

    class Observer:
        count = 0

        def update(self, *args):
            self.count += 1
            transition = "dog_enter" if self.count == 1 else "dog_exit" if self.count == 3 else None
            stay = 5.0 if transition == "dog_exit" else None
            return DogState(True, self.count < 3, transition, [], 1, tray_stay_seconds=stay)

    class Collector:
        def __init__(self):
            self.sources = []
            self.stays = []

        def poll(self):
            pass

        def trigger(self, event_id=None, source="detected", *, tray_stay_seconds=None):
            self.sources.append(source)
            self.stays.append(tray_stay_seconds)
            return "clip-id"

    class Notifier:
        def send(self, *, clip_id=None, detected_at=None):
            assert clip_id == "clip-id"
            return False

    collector = Collector()
    monitor(
        Camera(),
        ROI(0, 0, 1, 1),
        base,
        Config(data_dir=str(tmp_path), fps=100, verify_seconds=0.01),
        Notifier(),
        max_frames=8,
        collector=collector,
        dog_observer=Observer(),
    )
    assert collector.sources == ["dog_enter", "dog_exit", "detected"]
    assert collector.stays == [None, 5.0, None]
    assert len(Store(tmp_path).list()) == 1


def test_run_can_enable_dog_detection_without_editing_config():
    from poop_detector.main import parser

    assert parser().parse_args(["run", "--dog"]).dog


def test_scored_predictions_keep_the_confidence_of_each_box():
    from poop_detector.features.dog import DogDetector

    class Net:
        def setInput(self, blob):
            pass

        def forward(self):
            output = np.zeros((1, 8400, 85), dtype=np.float32)
            output[0, 0, :4] = [24, 24, np.log(48), np.log(32)]
            output[0, 0, 4] = 0.5
            output[0, 0, 5 + 16] = 0.6
            return output

    detector = DogDetector(None, confidence=0.2, net=Net())
    (scored,) = detector.predict_scored(np.zeros((100, 200, 3), np.uint8))
    assert scored.box == (0, 20, 120, 99)
    assert scored.score == pytest.approx(0.3)
    assert detector.predict(np.zeros((100, 200, 3), np.uint8)) == [scored.box]
