"""斜めのトイレ面の設定・保存と射影補正。"""

import cv2
import numpy as np
import pytest

from poop_detector.detection.roi import ROI


def test_polygon_roundtrip_scaling_and_warp(tmp_path):
    from poop_detector.detection.roi import PolygonROI

    points = [(20, 20), (80, 30), (70, 80), (30, 70)]
    roi = PolygonROI.from_pixels(points, 101, 101)
    path = tmp_path / "roi.json"
    roi.save(path)
    loaded = ROI.load(path)
    np.testing.assert_allclose(loaded.pixels(201, 201), np.array(points) * 2)
    frame = np.zeros((101, 101, 3), np.uint8)
    cv2.fillConvexPoly(frame, np.array(points), (100, 100, 100))
    crop = loaded.crop(frame)
    assert crop.shape[0] > 40
    assert crop.shape[1] > 40
    assert np.all(crop[5:-5, 5:-5] == 100)
    assert np.mean(crop[:, :, 0] == 0) < 0.01


@pytest.mark.parametrize(
    ("points", "message"),
    [
        ([(0, 0), (90, 90), (90, 0), (0, 90)], "交差やくぼみ"),
        ([(0, 0), (90, 0), (45, 10), (0, 90)], "交差やくぼみ"),
        ([(10, 10), (10, 10), (10, 10), (10, 10)], "交差やくぼみ"),
        ([(0, 0), (90, 0), (90, 90)], "正しい座標"),
        ([(-1, 0), (90, 0), (90, 90), (0, 90)], "画像の内側"),
        ([(0, 0), (float("nan"), 0), (90, 90), (0, 90)], "正しい座標"),
    ],
)
def test_invalid_polygon_is_rejected(points, message):
    from poop_detector.detection.roi import PolygonROI

    with pytest.raises(ValueError, match=message):
        PolygonROI.from_pixels(points, 101, 101)


def test_polygon_baseline_metadata_roundtrip(tmp_path):
    from poop_detector.detection.roi import PolygonROI
    from poop_detector.detection.scene import load_baseline, save_baseline

    roi = PolygonROI.from_pixels([(10, 10), (90, 20), (80, 90), (20, 80)], 101, 101)
    frame = np.full((101, 101, 3), 150, np.uint8)
    path = tmp_path / "empty.png"
    save_baseline(path, roi.crop(frame), roi)
    assert np.array_equal(load_baseline(path, ROI.load(save_roi(tmp_path, roi))), roi.crop(frame))
    with pytest.raises(ValueError, match="ROIが変わっています"):
        load_baseline(path, ROI(0, 0, 1, 1))


def save_roi(tmp_path, roi):
    path = tmp_path / "roi.json"
    roi.save(path)
    return path


def test_selector_four_clicks_and_cancel(monkeypatch):
    from poop_detector.detection.roi import select_polygon

    frame = np.zeros((101, 101, 3), np.uint8)
    callback = None

    def set_callback(window, handler):
        nonlocal callback
        callback = handler

    def enter(delay):
        for x, y in [(10, 10), (90, 20), (80, 90), (20, 80)]:
            callback(cv2.EVENT_LBUTTONDOWN, x, y, 0, None)
        return 13

    monkeypatch.setattr(cv2, "namedWindow", lambda *args: None)
    monkeypatch.setattr(cv2, "resizeWindow", lambda *args: None)
    monkeypatch.setattr(cv2, "setMouseCallback", set_callback)
    monkeypatch.setattr(cv2, "imshow", lambda *args: None)
    monkeypatch.setattr(cv2, "destroyWindow", lambda *args: None)
    monkeypatch.setattr(cv2, "getWindowProperty", lambda *args: 1)
    monkeypatch.setattr(cv2, "waitKey", enter)
    assert select_polygon(frame).crop(frame).size > 0
    monkeypatch.setattr(cv2, "waitKey", lambda *args: 27)
    with pytest.raises(ValueError, match="ROI選択を取り消しました"):
        select_polygon(frame)


def test_cli_accepts_polygon_option():
    from poop_detector.main import parser

    args = parser().parse_args(["setup-roi", "--polygon", "--replace"])
    assert args.polygon
    assert args.replace
