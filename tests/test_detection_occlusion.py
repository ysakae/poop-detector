"""ROIを横切る犬の体を小物体の出現と誤認しない。"""

import cv2
import numpy as np
import pytest

from poop_detector.detection import Detector
from poop_detector.state_machine import Machine


@pytest.mark.parametrize(
    "rect",
    [
        (0, 50, 20, 100),
        (179, 50, 199, 100),
        (50, 0, 100, 20),
        (50, 179, 100, 199),
    ],
)
def test_partial_body_at_roi_border_is_occupied(rect):
    baseline = np.full((200, 200, 3), 200, np.uint8)
    frame = baseline.copy()
    cv2.rectangle(frame, rect[:2], rect[2:], (30, 30, 30), -1)
    observation = Detector(baseline).analyze(frame)
    assert observation.score < 0.18
    assert observation.kind == "occupied"
    machine = Machine(verify_seconds=2)
    assert not machine.update(observation.kind, 0)
    assert not machine.update(observation.kind, 10)


def test_border_occlusion_blocks_interior_candidate_until_clear():
    baseline = np.full((200, 200, 3), 200, np.uint8)
    candidate = baseline.copy()
    cv2.rectangle(candidate, (80, 80), (100, 100), (30, 30, 30), -1)
    blocked = candidate.copy()
    cv2.rectangle(blocked, (0, 50), (20, 100), (30, 30, 30), -1)
    detector = Detector(baseline)
    machine = Machine(verify_seconds=2)
    assert not machine.update(detector.analyze(candidate).kind, 0)
    assert not machine.update(detector.analyze(blocked).kind, 2)
    assert not machine.update(detector.analyze(candidate).kind, 3)
    assert machine.update(detector.analyze(candidate).kind, 5)


def test_tiny_border_noise_does_not_hide_interior_object():
    baseline = np.full((200, 200, 3), 200, np.uint8)
    frame = baseline.copy()
    cv2.rectangle(frame, (80, 80), (100, 100), (30, 30, 30), -1)
    frame[0:2, 0:2] = 30
    assert Detector(baseline).analyze(frame).kind == "candidate"
