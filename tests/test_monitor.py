"""偽のカメラや人工の動画を監視ループに流し、検知から記録までを検証する。"""

import cv2
import pytest
from support import FrameSource, SilentNotifier, spot_scene

from poop_detector.camera import Camera
from poop_detector.config import Config
from poop_detector.events import Store
from poop_detector.monitoring import monitor
from poop_detector.roi import ROI


@pytest.mark.parametrize("review_notifications", [False, True])
def test_offline_pipeline_records_one_event(tmp_path, review_notifications):
    baseline, changed = spot_scene()

    class Notifier:
        def send(self):
            assert not review_notifications
            return True

    config = Config(data_dir=str(tmp_path), fps=100, verify_seconds=0.01)
    monitor(
        FrameSource(changed),
        ROI(0, 0, 1, 1),
        baseline,
        config,
        Notifier(),
        max_frames=8,
        review_notifications=review_notifications,
    )
    events = Store(tmp_path).list()
    assert len(events) == 1
    assert (events[0]["notified_at"] is None) == review_notifications


def test_real_decoder_to_event_pipeline(tmp_path):
    baseline, changed = spot_scene()
    path = tmp_path / "synthetic.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (200, 200))
    assert writer.isOpened()
    for _ in range(8):
        writer.write(changed)
    writer.release()

    config = Config(data_dir=str(tmp_path / "output"), fps=100, verify_seconds=0.01)
    with Camera(str(path)) as camera:
        assert (
            monitor(camera, ROI(0, 0, 1, 1), baseline, config, SilentNotifier(), max_frames=8) == 8
        )
    assert len(Store(config.data_dir).list()) == 1


def test_restart_after_cleaning_rearms(tmp_path):
    baseline, changed = spot_scene()
    config = Config(data_dir=str(tmp_path), fps=100, verify_seconds=0.01, clear_seconds=0.01)
    for frame in [changed, baseline, changed]:
        monitor(
            FrameSource(frame), ROI(0, 0, 1, 1), baseline, config, SilentNotifier(), max_frames=5
        )
    assert len(Store(tmp_path).list()) == 2


def test_waiting_for_new_frame_keeps_verification(tmp_path):
    baseline, changed = spot_scene()
    config = Config(data_dir=str(tmp_path), fps=100, verify_seconds=0.03)
    # 新しいフレームが1回おきにしか届かない。末尾は changed なので最後まで届き続ける。
    source = FrameSource(*[changed, None] * 5, changed)
    monitor(source, ROI(0, 0, 1, 1), baseline, config, SilentNotifier(), max_frames=6)
    assert len(Store(tmp_path).list()) == 1
