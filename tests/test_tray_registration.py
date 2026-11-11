"""稼働中のトレー登録と録画の区切りを確認する。"""

import numpy as np

from poop_detector.detection.roi import ROI
from poop_detector.features.tray_registration import TrayRegistration
from poop_detector.video.recording import ClipCollector


def test_tray_registration_switches_only_after_a_new_frame_is_validated(tmp_path):
    registration = TrayRegistration(tmp_path)
    roi = ROI(0.1, 0.1, 0.8, 0.8)
    frame = np.full((100, 100, 3), 120, dtype=np.uint8)

    registration.begin()
    assert registration.poll(None, roi, track_tray=False) is None
    assert registration.status()["phase"] == "paused"
    registration.apply()
    assert registration.poll(frame, roi, track_tray=False) is None
    assert registration.scene_root() == tmp_path

    scene = registration.poll(frame.copy(), roi, track_tray=False)
    assert scene is not None
    assert scene.roi == roi
    np.testing.assert_array_equal(scene.baseline, roi.crop(frame))
    assert registration.status()["phase"] == "applied"
    assert ROI.load(registration.scene_root() / "roi.json") == roi


def test_tray_registration_keeps_previous_scene_on_failure_and_cancel(tmp_path):
    registration = TrayRegistration(tmp_path)
    roi = ROI(0.1, 0.1, 0.8, 0.8)
    frame = np.full((100, 100, 3), 120, dtype=np.uint8)

    registration.begin()
    registration.poll(None, roi, track_tray=True)
    registration.apply(roi)
    assert registration.poll(frame, roi, track_tray=True) is None
    assert registration.status()["phase"] == "failed"
    assert registration.scene_root() == tmp_path

    registration.cancel()
    registration.poll(None, roi, track_tray=True)
    assert registration.status()["phase"] == "cancelled"
    assert registration.scene_root() == tmp_path


def test_recording_does_not_include_frames_from_tray_maintenance(tmp_path):
    frame = np.full((100, 100, 3), 120, dtype=np.uint8)
    samples = []

    def encode(frames, path, fps):
        samples.extend(sample[0] for sample in frames)
        path.touch()
        return len(frames) / fps

    with ClipCollector(tmp_path, after=1, encoder=encode) as collector:
        collector.feed(frame, now=1)
        old = collector.trigger(now=1)
        collector.set_maintenance(True)
        collector.feed(frame, now=2)
        assert collector.trigger(now=2) is None
        collector.set_maintenance(False)
        collector.feed(frame, now=3)
        new = collector.trigger(now=3)
        collector.feed(frame, now=4)
        collector.poll(now=4, force=True)
        collector.drain()
        assert collector.store.get(old)["interrupted"]
        assert collector.store.get(new)["status"] == "ready"

    assert samples == [1, 3, 4]
