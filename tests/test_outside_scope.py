"""トレー外検知の範囲は、トレー外の範囲を登録していなければサークルの床面を使うことを検証する。"""

import json

import numpy as np
import pytest

from poop_detector.commands.context import CommandContext, CommandError, load_outside_roi
from poop_detector.config import Config
from poop_detector.detection.roi import MaskedROI

FLOOR = [[0.0, 0.0], [0.7, 0.0], [0.7, 1.0], [0.0, 1.0]]
OUTSIDE = [[0.1, 0.1], [0.6, 0.1], [0.6, 0.9]]


def context(tmp_path):
    return CommandContext(Config(data_dir=str(tmp_path)))


def register_floor(root):
    data = {"id": "0" * 32, "mode": "mask", "points": FLOOR}
    (root / "activity-layout.json").write_text(json.dumps(data))


def test_floor_is_used_when_no_outside_area_is_registered(tmp_path):
    register_floor(tmp_path)
    assert np.allclose(load_outside_roi(context(tmp_path)).points, FLOOR)


def test_registered_outside_area_comes_first(tmp_path):
    register_floor(tmp_path)
    MaskedROI(OUTSIDE).save(tmp_path / "outside-roi.json")
    assert np.allclose(load_outside_roi(context(tmp_path)).points, OUTSIDE)


def test_neither_area_asks_for_the_floor(tmp_path):
    with pytest.raises(CommandError, match="サークルの床面"):
        load_outside_roi(context(tmp_path))
