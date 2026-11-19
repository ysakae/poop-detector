"""映像の画素の動きと犬検出の枠から、睡眠の判定に渡す1秒の観測を作る。"""

import math
from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from ..frames import Frame
from .dog import Box
from .sleep import Presence

# 動きを測る画像の幅（画素）。画面の大きさによらず同じ細かさで比べ、計算も軽くする。
MOTION_WIDTH = 160
# 縮小した灰色の画像で、前回との明るさの差がこれを超えた画素を「変化した」と数える。
# 平滑化したあとの値なので、夜の赤外線映像のざらつき（標準偏差6程度）では超えない。
PIXEL_DELTA = 25
# 犬の枠の中で変化した画素の割合がこれ未満なら、枠の中は静止とみなす。
STILL_RATIO = 0.02
# 床面の中で変化した画素の割合がこれ未満なら、床面は静かとみなす。
# 犬は床面に比べて小さいので、枠の中より低くして、犬1頭分の小さな動きも拾う。
FLOOR_STILL_RATIO = 0.002
# 枠の中心の移動が、枠の対角線のこの割合未満なら、同じ場所にいるとみなす。
STILL_SHIFT = 0.1
# 枠の中心から床面の縁までの距離（正規化した座標）がこれ未満で、縁に近づいていれば、
# 「縁に向かう動き」とみなす。
EDGE_MARGIN = 0.15
# 雑音を抑える平滑化の大きさ（画素、奇数）。
BLUR_SIZE = 5

_WHOLE_FRAME = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32)


def _small_gray(frame: Frame) -> NDArray[Any]:
    gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    size = (MOTION_WIDTH, max(1, round(height * MOTION_WIDTH / width)))
    small = cv2.resize(gray, size, interpolation=cv2.INTER_AREA)
    blurred: NDArray[Any] = cv2.GaussianBlur(small, (BLUR_SIZE, BLUR_SIZE), 0)
    return blurred


class MotionSampler:
    """床面の中の犬の枠と画素の変化から、still・moving・left・unknown を決める。

    犬の居場所は「出ていった証拠」（縁に向かう動きや、動いた直後の床面の変化）があるまで保ち、
    検出が外れただけでは外に出たとみなさない。居場所が分からないときは unknown にする。
    """

    def __init__(self, boundary: NDArray[Any] | None) -> None:
        self.boundary = _WHOLE_FRAME if boundary is None else np.asarray(boundary, dtype=np.float32)
        self._floor: NDArray[Any] | None = None
        self.reset()

    def reset(self) -> None:
        """居場所と前回の画像を忘れる（監視の開始や接続の再開）。"""
        self._previous: NDArray[Any] | None = None
        self._location: Box | None = None
        # 床面の中で最後に決めた観測（still か moving）
        self._last: Presence = "unknown"
        self._toward_edge = False
        self._left = False

    def sample(self, frame: Frame, boxes: Sequence[Box]) -> tuple[Presence, bool]:
        """1秒分の観測と、床面の中の犬検出がちょうど1つ当たったかを返す。"""
        gray = _small_gray(frame)
        changed = self._changed(gray)
        self._previous = gray
        inside = self._inside(boxes, frame.shape[:2])
        if len(inside) > 1:
            return "unknown", False
        if inside:
            return self._with_box(inside[0], changed, frame.shape[:2]), True
        return self._without_box(changed), False

    def _changed(self, gray: NDArray[Any]) -> NDArray[Any] | None:
        previous = self._previous
        if previous is None or previous.shape != gray.shape:
            return None
        changed: NDArray[Any] = cv2.absdiff(gray, previous) > PIXEL_DELTA
        return changed

    def _inside(self, boxes: Sequence[Box], shape: tuple[int, ...]) -> list[Box]:
        height, width = shape
        if min(height, width) < 2:
            return []
        return [box for box in boxes if self._edge_distance(box, shape) >= 0]

    def _edge_distance(self, box: Box, shape: tuple[int, ...]) -> float:
        """枠の中心から床面の縁までの距離。床面の外なら負になる。"""
        height, width = shape
        x1, y1, x2, y2 = box
        center = ((x1 + x2) / (2 * (width - 1)), (y1 + y2) / (2 * (height - 1)))
        return float(cv2.pointPolygonTest(self.boundary, center, True))

    def _with_box(self, box: Box, changed: NDArray[Any] | None, shape: tuple[int, ...]) -> Presence:
        previous = self._location
        presence: Presence = "moving"
        if previous is not None and changed is not None:
            shift = math.dist(_center(box), _center(previous))
            diagonal = math.hypot(box[2] - box[0], box[3] - box[1])
            if shift < diagonal * STILL_SHIFT and _box_ratio(changed, box, shape) < STILL_RATIO:
                presence = "still"
        distance = self._edge_distance(box, shape)
        approaching = previous is None or distance < self._edge_distance(previous, shape)
        self._toward_edge = presence == "moving" and distance < EDGE_MARGIN and approaching
        self._location, self._last, self._left = box, presence, False
        return presence

    def _without_box(self, changed: NDArray[Any] | None) -> Presence:
        quiet = changed is not None and self._floor_ratio(changed) < FLOOR_STILL_RATIO
        if self._location is None:
            if quiet and self._left:
                return "left"
            self._left = False
            return "unknown"
        if self._last == "moving" and (self._toward_edge or not quiet):
            self._location, self._last, self._toward_edge = None, "unknown", False
            self._left = True
            return "left"
        if quiet:
            return self._last
        # 静止していた犬の検出が外れて床面が動いた。出ていった証拠はないので居場所は保つ。
        return "unknown"

    def _floor_ratio(self, changed: NDArray[Any]) -> float:
        if self._floor is None or self._floor.shape != changed.shape:
            height, width = changed.shape
            points = np.round(self.boundary * [width - 1, height - 1]).astype(np.int32)
            floor = np.zeros(changed.shape, dtype=np.uint8)
            cv2.fillPoly(floor, [points], 1)
            self._floor = floor.astype(bool)
        area = int(self._floor.sum())
        if area == 0:
            return 1.0
        return float(changed[self._floor].sum()) / area


def _center(box: Box) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return (x1 + x2) / 2, (y1 + y2) / 2


def _box_ratio(changed: NDArray[Any], box: Box, shape: tuple[int, ...]) -> float:
    """枠の中で変化した画素の割合。枠は元の画像の座標なので、縮小した画像の座標へ移す。"""
    height, width = shape
    small_height, small_width = changed.shape
    x1, y1, x2, y2 = box
    left = max(0, min(small_width - 1, math.floor(x1 * small_width / width)))
    top = max(0, min(small_height - 1, math.floor(y1 * small_height / height)))
    right = max(left + 1, min(small_width, math.ceil(x2 * small_width / width)))
    bottom = max(top + 1, min(small_height, math.ceil(y2 * small_height / height)))
    return float(changed[top:bottom, left:right].mean())
