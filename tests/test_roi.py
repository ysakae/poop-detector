"""矩形 ROI の保存・読み込みと座標の換算を検証する。"""

import numpy as np
import pytest

from poop_detector.roi import ROI


def test_roi_roundtrip_and_scaling(tmp_path):
    roi = ROI.from_pixels((20, 40, 100, 80), 200, 200)
    path = tmp_path / "roi.json"
    roi.save(path)
    assert ROI.load(path).pixels(400, 400) == (40, 80, 200, 160)
    with pytest.raises(ValueError, match="空でない領域"):
        ROI.from_pixels((0, 0, 0, 20), 200, 200)


def test_only_masked_roi_limits_monitored_pixels():
    from poop_detector.roi import MaskedROI, PolygonROI

    shape = (100, 100, 3)
    assert ROI(0, 0, 1, 1).valid_mask(shape) is None
    assert PolygonROI([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]).valid_mask(shape) is None
    masked = MaskedROI([[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]])
    mask = masked.valid_mask(shape)
    assert mask is not None
    assert mask.shape == masked.crop(np.zeros(shape, np.uint8)).shape[:2]
    assert 0 < np.count_nonzero(mask) < mask.size
