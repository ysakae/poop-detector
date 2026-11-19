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
# 床面の中で、つながった変化の塊の面積がこれ以上あれば「床面が動いた」。面積は幅
# MOTION_WIDTH に縮小した画像の画素で数える。散らばった点の合計ではなく塊の大きさで決め、
# 圧縮のブロック雑音や赤外線のざらつき（1点は平滑化後に1画素ほど）を除く。人工画像では、
# 縮小画像で4×6画素の耳の動きが平滑化で広がって約44画素の塊になり、これを拾える。
MIN_MOTION_AREA = 24
# 検出が外れたあと、最後の枠の中の画素のうちこの割合より多くが、最後に検出したときの
# 枠の中の画像と PIXEL_DELTA を超えて違えば、犬はもう枠にいないとみなす。
# 検出の枠には背景も入るので、半分より少し低くする。身じろぎ（枠の数%）では超えない。
BOX_GONE_RATIO = 0.4
# 枠の中心の移動が、枠の対角線のこの割合未満なら、同じ場所にいるとみなす。
STILL_SHIFT = 0.1
# 検出が外れたとき、最後の枠を幅・高さのこの割合だけ四方に広げた範囲の中の動きは、
# 居場所での身じろぎとみなす。
STIR_MARGIN = 0.25
# 床面の縁から内側へ、縮小した画像の幅のこの割合の帯。検出が外れたまま、
# 最後の枠の外の動きがこの帯に届いたら、出ていったとみなす。
EDGE_MARGIN = 0.1
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

    犬の居場所は「出ていった証拠」があるまで保ち、検出が外れただけでは外に出たとみなさない。
    居場所があって検出が外れたとき:
    - 最後の枠の中が、最後に見えた犬の画像と大きく違う（BOX_GONE_RATIO）→ 居場所を捨てる。
      枠が床面の縁の帯（境界がなければ画面の端）にかかっていれば left、ほかは unknown
    - 床面が静か → still（動いていない証拠）
    - 動きが最後の枠（STIR_MARGIN だけ広げた範囲）の中だけ → moving（身じろぎ）
    - 枠の外の動きが床面の縁の帯に届いた → left
    left のあとは、床面が静かな間と、動きが出口（最後の枠を広げた範囲と縁の帯）の中だけの間は
    left を保つ（出ていく途中の残りの動き）。
    - ほかの動き → 居場所を捨てて unknown。再び検出するまで still には戻さない
    """

    def __init__(self, boundary: NDArray[Any] | None) -> None:
        self.boundary = _WHOLE_FRAME if boundary is None else np.asarray(boundary, dtype=np.float32)
        self._areas: tuple[NDArray[Any], NDArray[Any]] | None = None
        self.reset()

    def reset(self) -> None:
        """居場所と前回の画像を忘れる（監視の開始や接続の再開）。"""
        self._previous: NDArray[Any] | None = None
        self._location: Box | None = None
        # 最後に検出したときの、枠の中の縮小した灰色の画像
        self._snapshot: NDArray[Any] | None = None
        # left にしたときの出口（縮小した画像のマスク）。left でなければ None
        self._exit: NDArray[Any] | None = None

    def sample(self, frame: Frame, boxes: Sequence[Box]) -> tuple[Presence, bool]:
        """1秒分の観測と、床面の中の犬検出がちょうど1つ当たったかを返す。"""
        gray = _small_gray(frame)
        changed = self._changed(gray)
        self._previous = gray
        shape = frame.shape[:2]
        inside = self._inside(boxes, shape)
        if len(inside) > 1:
            return "unknown", False
        if inside:
            presence = self._with_box(inside[0], changed, shape)
            self._snapshot = gray[_box_slices(inside[0], shape, gray.shape, 0)].copy()
            return presence, True
        return self._without_box(gray, changed, shape), False

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
        return [box for box in boxes if self._contains(box, shape)]

    def _contains(self, box: Box, shape: tuple[int, ...]) -> bool:
        """枠の中心が床面の中にあるか。"""
        height, width = shape
        x1, y1, x2, y2 = box
        center = ((x1 + x2) / (2 * (width - 1)), (y1 + y2) / (2 * (height - 1)))
        return cv2.pointPolygonTest(self.boundary, center, False) >= 0

    def _with_box(self, box: Box, changed: NDArray[Any] | None, shape: tuple[int, ...]) -> Presence:
        previous = self._location
        self._location, self._exit = box, None
        if previous is None or changed is None:
            return "moving"
        shift = math.dist(_center(box), _center(previous))
        diagonal = math.hypot(box[2] - box[0], box[3] - box[1])
        region = changed[_box_slices(box, shape, changed.shape, 0)]
        if shift < diagonal * STILL_SHIFT and float(region.mean()) < STILL_RATIO:
            return "still"
        return "moving"

    def _without_box(
        self, gray: NDArray[Any], changed: NDArray[Any] | None, shape: tuple[int, ...]
    ) -> Presence:
        motion = None if changed is None else self._motion(changed)
        quiet = motion is not None and not motion.any()
        location = self._location
        if location is None:
            return self._after_left(motion)
        region = _box_slices(location, shape, gray.shape, 0)
        if self._gone(gray[region]):
            # 枠が縁にかかっていれば、検出が外れたまま縁の外へ出ていったとみなす。
            if self._regions(gray.shape)[1][region].any():
                return self._leave(location, shape, gray.shape)
            self._location, self._snapshot = None, None
            return "unknown"
        if quiet:
            return "still"
        if motion is not None:
            stir = np.zeros(motion.shape, dtype=bool)
            stir[_box_slices(location, shape, motion.shape, STIR_MARGIN)] = True
            outside = motion & ~stir
            if not outside.any():
                return "moving"
            if (outside & self._regions(motion.shape)[1]).any():
                return self._leave(location, shape, motion.shape)
        # 居場所の外で動きがあった（または画像を比べられない）。どこにいるか分からない。
        self._location, self._snapshot = None, None
        return "unknown"

    def _leave(self, location: Box, shape: tuple[int, ...], small: tuple[int, ...]) -> Presence:
        exit_area = self._regions(small)[1].copy()
        exit_area[_box_slices(location, shape, small, STIR_MARGIN)] = True
        self._location, self._snapshot, self._exit = None, None, exit_area
        return "left"

    def _after_left(self, motion: NDArray[Any] | None) -> Presence:
        """居場所がないときの観測。出ていった直後の出口の中の動きと、静かな床面は left を保つ。"""
        exit_area = self._exit
        inside_exit = (
            exit_area is not None
            and motion is not None
            and exit_area.shape == motion.shape
            and not (motion & ~exit_area).any()
        )
        if inside_exit:
            return "left"
        self._exit = None
        return "unknown"

    def _gone(self, region: NDArray[Any]) -> bool:
        """最後に見えた犬の画像と比べ、枠の中の大部分が変わったか。"""
        snapshot = self._snapshot
        if snapshot is None or snapshot.shape != region.shape or region.size == 0:
            return False
        differ = cv2.absdiff(region, snapshot) > PIXEL_DELTA
        return float(differ.mean()) > BOX_GONE_RATIO

    def _motion(self, changed: NDArray[Any]) -> NDArray[Any]:
        """床面の中で、MIN_MOTION_AREA 以上の塊になった変化の画素。"""
        floor, _ = self._regions(changed.shape)
        mask = (changed & floor).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        large = [
            label for label in range(1, count) if stats[label, cv2.CC_STAT_AREA] >= MIN_MOTION_AREA
        ]
        motion: NDArray[Any] = np.isin(labels, large)
        return motion

    def _regions(self, shape: tuple[int, ...]) -> tuple[NDArray[Any], NDArray[Any]]:
        """縮小した画像での（床面, 床面の縁の帯）のマスク。"""
        if self._areas is None or self._areas[0].shape != shape:
            height, width = shape
            points = np.round(self.boundary * [width - 1, height - 1]).astype(np.int32)
            floor = np.zeros(shape, dtype=np.uint8)
            cv2.fillPoly(floor, [points], 1)
            # 画面の端も床面の縁として扱うため、外周を0で囲んでから距離を測る。
            padded = cv2.copyMakeBorder(floor, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
            distance = cv2.distanceTransform(padded, cv2.DIST_L2, 3)[1:-1, 1:-1]
            edge = (floor > 0) & (distance <= EDGE_MARGIN * width)
            self._areas = (floor > 0, edge)
        return self._areas


def _center(box: Box) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return (x1 + x2) / 2, (y1 + y2) / 2


def _box_slices(
    box: Box, shape: tuple[int, ...], small: tuple[int, ...], margin: float
) -> tuple[slice, slice]:
    """元の画像の座標の枠を、四方に margin（幅・高さの割合）広げて縮小した画像の範囲にする。"""
    height, width = shape
    small_height, small_width = small
    x1, y1, x2, y2 = box
    dx, dy = (x2 - x1) * margin, (y2 - y1) * margin
    left = max(0, min(small_width - 1, math.floor((x1 - dx) * small_width / width)))
    top = max(0, min(small_height - 1, math.floor((y1 - dy) * small_height / height)))
    right = max(left + 1, min(small_width, math.ceil((x2 + dx) * small_width / width)))
    bottom = max(top + 1, min(small_height, math.ceil((y2 + dy) * small_height / height)))
    return slice(top, bottom), slice(left, right)
