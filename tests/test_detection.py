"""基準画像との差分による候補・遮蔽・不安定の判定を検証する。"""

import numpy as np
import pytest
from support import spot_scene

from poop_detector.detection import Detector


def test_detection_small_object_and_illumination():
    baseline, changed = spot_scene()
    detector = Detector(baseline)
    assert detector.analyze(baseline).kind == "empty"
    assert detector.analyze(changed).kind == "candidate"
    assert detector.analyze(np.full_like(baseline, 150)).kind == "unstable"


def test_occlusion_is_not_feces():
    baseline, _ = spot_scene()
    changed = baseline.copy()
    changed[30:170, 30:170] = 10
    assert Detector(baseline).analyze(changed).kind == "occupied"


def test_resolution_mismatch_rejected():
    baseline, _ = spot_scene()
    with pytest.raises(ValueError):
        Detector(baseline).analyze(baseline[:100])
