"""尿跡を残した追加検知と除去・再起動を検証する。"""

import cv2
import numpy as np

from poop_detector.config import Config
from poop_detector.events import Store
from poop_detector.roi import ROI


def scenes():
    clean = np.full((240, 240, 3), 200, np.uint8)
    first = clean.copy()
    cv2.rectangle(first, (40, 40), (65, 65), (30, 30, 30), -1)
    second = first.copy()
    cv2.rectangle(second, (130, 130), (155, 155), (30, 30, 30), -1)
    return clean, first, second


def test_known_stain_new_stain_removal_and_growth():
    from poop_detector.detection import IncrementalDetector

    clean, first, second = scenes()
    detector = IncrementalDetector(clean)
    assert detector.analyze(first).kind == "candidate"
    detector.accept(first)
    assert detector.analyze(first).kind == "empty"
    assert detector.analyze(second).kind == "candidate"
    detector.accept(second)
    assert detector.analyze(first).kind == "empty"
    assert detector.analyze(clean).kind == "empty"
    expanded = first.copy()
    cv2.rectangle(expanded, (40, 40), (85, 85), (30, 30, 30), -1)
    detector.accept(first)
    assert detector.analyze(expanded).kind == "candidate"


def test_addition_on_existing_stain_and_occlusion():
    from poop_detector.detection import IncrementalDetector

    clean, first, _ = scenes()
    first[40:66, 40:66] = 130
    detector = IncrementalDetector(clean)
    detector.accept(first)
    darker = first.copy()
    darker[40:66, 40:66] = 20
    assert detector.analyze(darker).kind == "candidate"
    blocked = darker.copy()
    blocked[:100, :100] = 0
    assert detector.analyze(blocked).kind == "occupied"


def run_frames(root, frames):
    from poop_detector.monitoring import monitor

    clean, _, _ = scenes()

    class Camera:
        generation = 1
        healthy = True

        def read(self):
            return next(self.frames)

        def __init__(self):
            self.frames = iter(frames)

    class Silent:
        def send(self):
            return False

    config = Config(data_dir=str(root), fps=100, verify_seconds=0.01, clear_seconds=0.01)
    monitor(Camera(), ROI(0, 0, 1, 1), clean, config, Silent(), max_frames=len(frames))


def test_pipeline_multiple_events_and_restart(tmp_path):
    clean, first, second = scenes()
    run_frames(tmp_path, [first] * 5 + [second] * 5)
    assert len(Store(tmp_path).list()) == 2
    run_frames(tmp_path, [second] * 5)
    assert len(Store(tmp_path).list()) == 2
    run_frames(tmp_path, [clean] * 5)
    run_frames(tmp_path, [first] * 5)
    assert len(Store(tmp_path).list()) == 3


def test_partial_cleaning_then_same_location_reused(tmp_path):
    clean, first, second = scenes()
    other = clean.copy()
    other[130:156, 130:156] = 30
    run_frames(tmp_path, [first] * 5 + [second] * 5 + [other] * 5 + [second] * 5)
    assert len(Store(tmp_path).list()) == 3


def test_large_partial_cleaning_does_not_block_later_addition(tmp_path):
    clean, _, _ = scenes()
    frame = clean.copy()
    frames = []
    positions = [(x, y) for y in (30, 95, 160) for x in (30, 95, 160)]
    for x, y in positions:
        frame[y : y + 40, x : x + 40] = 30
        frames += [frame.copy()] * 5
    for x, y in positions[:7]:
        frame[y : y + 40, x : x + 40] = 200
    frames += [frame.copy()] * 5
    x, y = positions[0]
    frame[y : y + 40, x : x + 40] = 30
    frames += [frame.copy()] * 5
    run_frames(tmp_path, frames)
    assert len(Store(tmp_path).list()) == 10


def test_persisted_reference_is_lossless_and_scoped_to_baseline(tmp_path):
    _, first, _ = scenes()
    store = Store(tmp_path)
    store.save_reference(first, "baseline-and-roi-one")
    np.testing.assert_array_equal(store.load_reference("baseline-and-roi-one"), first)
    assert store.load_reference("another-baseline-or-roi") is None


def test_moving_candidate_does_not_accumulate_verification(tmp_path):
    _, first, second = scenes()
    run_frames(tmp_path, [first, second] * 6)
    assert Store(tmp_path).list() == []
