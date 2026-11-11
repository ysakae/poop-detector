"""任意の多角形の内側だけを監視する。"""

import cv2
import numpy as np
import pytest

from poop_detector.detection import Detector
from poop_detector.roi import ROI

HEXAGON = [(50, 10), (90, 30), (90, 70), (50, 90), (10, 70), (10, 30)]


def test_hexagon_roundtrip_excludes_outside_and_scales(tmp_path):
    from poop_detector.roi import MaskedROI

    roi = MaskedROI.from_pixels(HEXAGON, 101, 101)
    path = tmp_path / "roi.json"
    roi.save(path)
    loaded = ROI.load(path)
    assert isinstance(loaded, MaskedROI)
    np.testing.assert_allclose(loaded.pixels(201, 201), np.array(HEXAGON) * 2)
    frame = np.full((101, 101, 3), 180, np.uint8)
    baseline = loaded.crop(frame)
    changed = frame.copy()
    changed[10:20, 10:20] = 10
    assert np.array_equal(baseline, loaded.crop(changed))
    changed[45:55, 45:55] = 10
    assert (
        Detector(baseline).analyze(loaded.crop(changed), loaded.mask(frame.shape)).kind
        == "candidate"
    )


def test_concave_polygon_is_supported():
    from poop_detector.roi import MaskedROI

    roi = MaskedROI.from_pixels([(10, 10), (90, 10), (90, 90), (50, 50), (10, 90)], 101, 101)
    mask = roi.mask((101, 101, 3))
    assert mask[10, 10] > 0
    assert mask[75, 40] == 0


@pytest.mark.parametrize(
    ("points", "message"),
    [
        ([(0, 0), (90, 90), (90, 0), (0, 90)], "面積のない多角形"),
        ([(0, 0), (90, 0)], "3点以上"),
        ([(0, 0), (90, 0), (90, 90), (0, 0)], "面積のない多角形"),
        ([(0, 0), (float("nan"), 0), (90, 90)], "3点以上"),
        ([(0, 0), (50, 50), (90, 90)], "面積のない多角形"),
    ],
)
def test_invalid_masked_polygon_is_rejected(points, message):
    from poop_detector.roi import MaskedROI

    with pytest.raises(ValueError, match=message):
        MaskedROI.from_pixels(points, 101, 101)


def test_polygon_boundary_occlusion_and_lighting():
    from poop_detector.roi import MaskedROI

    roi = MaskedROI.from_pixels(HEXAGON, 101, 101)
    baseline = np.full((101, 101, 3), 180, np.uint8)
    frame = baseline.copy()
    cv2.rectangle(frame, (22, 15), (37, 32), (10, 10, 10), -1)
    detector = Detector(roi.crop(baseline))
    assert detector.analyze(roi.crop(frame), roi.mask(frame.shape)).kind == "occupied"
    dimmed = np.full_like(frame, 100)
    assert detector.analyze(roi.crop(dimmed), roi.mask(frame.shape)).kind == "unstable"


def test_masked_baseline_metadata_is_distinct_from_warp(tmp_path):
    from poop_detector.roi import MaskedROI, PolygonROI
    from poop_detector.scene import load_baseline, save_baseline

    points = [(10, 10), (90, 10), (90, 90), (10, 90)]
    roi = MaskedROI.from_pixels(points, 101, 101)
    path = tmp_path / "empty.png"
    frame = np.full((101, 101, 3), 180, np.uint8)
    save_baseline(path, roi.crop(frame), roi)
    assert np.array_equal(load_baseline(path, roi), roi.crop(frame))
    with pytest.raises(ValueError, match="ROIが変わっています"):
        load_baseline(path, PolygonROI.from_pixels(points, 101, 101))


def test_selector_accepts_six_vertices(monkeypatch):
    from poop_detector.roi import select_polygon

    callback = None

    def set_callback(window, handler):
        nonlocal callback
        callback = handler

    def enter(delay):
        for x, y in HEXAGON:
            callback(cv2.EVENT_LBUTTONDOWN, x, y, 0, None)
        return 13

    for name in ("namedWindow", "resizeWindow", "imshow", "destroyWindow"):
        monkeypatch.setattr(cv2, name, lambda *args: None)
    monkeypatch.setattr(cv2, "setMouseCallback", set_callback)
    monkeypatch.setattr(cv2, "waitKey", enter)
    monkeypatch.setattr(cv2, "getWindowProperty", lambda *args: 1)
    assert len(select_polygon(np.zeros((101, 101, 3), np.uint8)).points) == 6
