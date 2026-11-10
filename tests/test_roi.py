"""矩形 ROI の保存・読み込みと座標の換算を検証する。"""

import pytest

from poop_detector.roi import ROI


def test_roi_roundtrip_and_scaling(tmp_path):
    roi = ROI.from_pixels((20, 40, 100, 80), 200, 200)
    path = tmp_path / "roi.json"
    roi.save(path)
    assert ROI.load(path).pixels(400, 400) == (40, 80, 200, 160)
    with pytest.raises(ValueError):
        ROI.from_pixels((0, 0, 0, 20), 200, 200)
