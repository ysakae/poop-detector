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
# 検出が外れたまま犬が動いたとみなし、居場所をその動きに移す最小の面積（縮小画像の画素）。
# 丸まった犬は検出が当たらないため、動いて止まった場所を居場所にする。おもちゃなど小さな
# 物の動き（人工画像の6×6の塊で約60画素）では移さない。昨晩の実映像では、寝返りで約80画素、
# 歩くと数百画素の塊になった。
MIN_RELOCATE_AREA = 72
# 床面の画素のうちこの割合より多くが一度に変わったら、犬の動きではなく明るさの切り替え
# （カメラの夜間モードへの切り替えや露出の調整、照明）とみなす。実映像では、夜間モードへの
# 切り替えで床面のほぼ100%が変わり、それが約15秒の間に数回起きた。犬が歩いても1割ほど。
LIGHTING_CHANGE_RATIO = 0.5
# 犬の大きさを（幅, 高さ）の画面に対する割合で。動きから居場所を作るとき、寝返りの一部分だけの
# 小さな塊を犬の大きさの枠に広げる。検出が当たれば、その枠の大きさに置き換える。
# 既定値は、俯瞰の実映像で横になった犬の検出枠（1280×720で約260×290画素）から決めた。
DOG_SIZE = (0.2, 0.4)
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
# left にしたあと、出口の中の動きを出ていく途中の残りの動きとみなす秒数（sample の回数）。
# これを過ぎたら、静かな床面だけ left を保ち、動きは戻ってきた証拠かもしれないので unknown。
EXIT_GRACE_SECONDS = 30
# 出口に含める縁の帯は、最後の枠を幅・高さのこの割合だけ四方に広げた範囲の中だけにする。
# 縁の帯のうち出ていった場所から離れた部分の動きは、出口の動きとみなさない。
EXIT_REACH = 1.0
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
    - 枠の外の MIN_RELOCATE_AREA 以上の動き → 犬がそこへ動いたとみなし、居場所を移して moving
      （丸まった犬は検出が当たらないため）。止まった最初の静かな場面の映像を犬の姿として覚える
    - ほかの小さな動き → 居場所を保ったまま moving
    最後の枠の姿が消えたとき、枠の外への動きがなければ居場所を捨てて unknown にする。
    left のあとは、床面が静かな間は left を保つ。left にしてから EXIT_GRACE_SECONDS の間は、
    動きが出口（最後の枠を広げた範囲と、その近くの縁の帯）の中だけでも left を保つ
    （出ていく途中の残りの動き）。猶予の間のほかの動きは unknown にする。猶予のあとの
    MIN_RELOCATE_AREA 以上の動きは、戻ってきた犬とみなして居場所にする。
    """

    def __init__(self, boundary: NDArray[Any] | None) -> None:
        self.boundary = _WHOLE_FRAME if boundary is None else np.asarray(boundary, dtype=np.float32)
        self._areas: tuple[NDArray[Any], NDArray[Any]] | None = None
        # 直近に検出した犬の枠の大きさ（画面に対する割合）。reset でも忘れない
        self._dog_size = DOG_SIZE
        self.reset()

    def reset(self) -> None:
        """居場所と前回の画像を忘れる（監視の開始や接続の再開）。"""
        self._previous: NDArray[Any] | None = None
        self._location: Box | None = None
        # 最後に検出したときの、枠の中の縮小した灰色の画像
        self._snapshot: NDArray[Any] | None = None
        # left にしたときの出口（縮小した画像のマスク）。left でなければ None
        self._exit: NDArray[Any] | None = None
        # left にしてから数えた sample の回数（出口の猶予を測る）。最初は猶予の外にする
        self._left_age = EXIT_GRACE_SECONDS + 1
        # 直近の元の画像の（高さ, 幅）
        self._shape: tuple[int, int] | None = None

    def sample(self, frame: Frame, boxes: Sequence[Box]) -> tuple[Presence, bool]:
        """1秒分の観測と、床面の中の犬検出がちょうど1つ当たったかを返す。"""
        self._left_age += 1
        self._shape = (frame.shape[0], frame.shape[1])
        gray = _small_gray(frame)
        changed = self._changed(gray)
        self._previous = gray
        shape = frame.shape[:2]
        inside = self._inside(boxes, shape)
        if len(inside) > 1:
            return "unknown", False
        if inside:
            x1, y1, x2, y2 = inside[0]
            self._dog_size = ((x2 - x1) / shape[1], (y2 - y1) / shape[0])
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
        """枠の中心か足元（下端の中央）が床面の中にあるか。

        柵に寄りかかって寝ると、上から写した体が柵の側に伸び、枠の中心が床面の外に出る。
        """
        height, width = shape
        x1, y1, x2, y2 = box
        x = (x1 + x2) / (2 * (width - 1))
        points = ((x, (y1 + y2) / (2 * (height - 1))), (x, y2 / (height - 1)))
        return any(cv2.pointPolygonTest(self.boundary, point, False) >= 0 for point in points)

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
        location = self._location
        if changed is not None and self._lighting_changed(changed):
            # 居場所と出口は保ち、切り替え後の映像を犬の姿として覚え直す。この1秒は判断できない。
            if location is not None:
                self._snapshot = gray[_box_slices(location, shape, gray.shape, 0)].copy()
            return "unknown"
        motion = None if changed is None else self._motion(changed)
        quiet = motion is not None and not motion.any()
        if location is None:
            return self._after_left(motion)
        region = _box_slices(location, shape, gray.shape, 0)
        outside = None
        if motion is not None:
            stir = np.zeros(motion.shape, dtype=bool)
            stir[_box_slices(location, shape, motion.shape, STIR_MARGIN)] = True
            outside = motion & ~stir
        if self._gone(gray[region]):
            # 枠が縁にかかっていれば、検出が外れたまま縁の外へ出ていったとみなす。
            if self._regions(gray.shape)[1][region].any():
                return self._leave(location, shape, gray.shape)
            # 枠の外へ動いた先があれば、そこへ移った。動きを伴わずに消えたなら分からない。
            if outside is not None and motion is not None and _area(outside) >= MIN_RELOCATE_AREA:
                return self._relocate(motion, shape, outside)
            self._location, self._snapshot = None, None
            return "unknown"
        if quiet:
            if self._snapshot is None:
                # 動きで追った居場所は、止まった最初の場面の映像を犬の姿として覚える。
                self._snapshot = gray[region].copy()
            return "still"
        if motion is not None and outside is not None:
            if not outside.any():
                return "moving"
            if (outside & self._regions(motion.shape)[1]).any():
                return self._leave(location, shape, motion.shape)
            if _area(outside) >= MIN_RELOCATE_AREA:
                return self._relocate(motion, shape, outside)
            # 小さな物の動きでは居場所を移さないが、静止とも数えない。
            return "moving"
        # 画像を比べられない。どこにいるか分からない。
        self._location, self._snapshot = None, None
        return "unknown"

    def _relocate(
        self, motion: NDArray[Any], shape: tuple[int, ...], seed: NDArray[Any] | None = None
    ) -> Presence:
        """検出されないまま動いた犬の居場所を、seed につながる動きの塊へ移す。

        少しずれただけの犬の差分は、新しく覆った前の側と空いた後ろの側の離れた塊になる。
        前の側（seed につながる塊）だけでは体の一部しか囲めないので、直前の枠の大きさを保ち、
        動いた向きの先端に合わせて置く。居場所がなければ動きの塊を犬の大きさまで広げる。
        """
        if seed is not None:
            _, labels = cv2.connectedComponents(motion.astype(np.uint8), connectivity=8)
            touched = np.unique(labels[seed & motion])
            motion = np.isin(labels, touched[touched > 0])
        ys, xs = np.nonzero(motion)
        height, width = shape
        small_height, small_width = motion.shape
        box: Box = (
            math.floor(xs.min() * width / small_width),
            math.floor(ys.min() * height / small_height),
            math.ceil((xs.max() + 1) * width / small_width) - 1,
            math.ceil((ys.max() + 1) * height / small_height) - 1,
        )
        previous = self._location
        if seed is not None and previous is not None:
            box = _keep_size(box, previous, shape)
        else:
            box = _grow(box, self._dog_size, shape)
        self._location = box
        # 止まるまでは犬の姿が分からないので、静かになった最初の場面で覚える。
        self._snapshot, self._exit = None, None
        return "moving"

    def _leave(self, location: Box, shape: tuple[int, ...], small: tuple[int, ...]) -> Presence:
        near = np.zeros(small, dtype=bool)
        near[_box_slices(location, shape, small, EXIT_REACH)] = True
        exit_area = self._regions(small)[1] & near
        exit_area[_box_slices(location, shape, small, STIR_MARGIN)] = True
        self._location, self._snapshot, self._exit = None, None, exit_area
        self._left_age = 0
        return "left"

    def _after_left(self, motion: NDArray[Any] | None) -> Presence:
        """居場所がないときの観測。静かな床面と、出ていった直後の出口の中の動きは left を保つ。"""
        exit_area = self._exit
        if exit_area is None or motion is None or exit_area.shape != motion.shape:
            return self._enter(motion)
        if not motion.any():
            return "left"
        if self._left_age <= EXIT_GRACE_SECONDS and not (motion & ~exit_area).any():
            return "left"
        return self._enter(motion)

    def _enter(self, motion: NDArray[Any] | None) -> Presence:
        """居場所が分からないときの床面の動き。犬らしい大きさなら、そこに犬がいるとみなす。"""
        self._exit = None
        # 出ていった直後の動きは、出ていく途中の動きかもしれないので犬の居場所にしない。
        if self._left_age <= EXIT_GRACE_SECONDS:
            return "unknown"
        if motion is not None and _area(motion) >= MIN_RELOCATE_AREA and self._shape is not None:
            return self._relocate(motion, self._shape)
        return "unknown"

    def _lighting_changed(self, changed: NDArray[Any]) -> bool:
        """床面の大部分が一度に変わったか（明るさの切り替え）。"""
        floor, _ = self._regions(changed.shape)
        return bool(floor.any()) and float(changed[floor].mean()) > LIGHTING_CHANGE_RATIO

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


def _area(mask: NDArray[Any]) -> int:
    return int(np.count_nonzero(mask))


def _grow(box: Box, size: tuple[float, float], shape: tuple[int, ...]) -> Box:
    """枠を、中心を保ったまま size（画面に対する割合）以上に広げる。"""
    height, width = shape

    def span(lo: int, hi: int, minimum: float, limit: int) -> tuple[int, int]:
        half = max(hi - lo, round(minimum * limit)) / 2
        center = (lo + hi) / 2
        return max(0, math.floor(center - half)), min(limit - 1, math.ceil(center + half))

    x1, x2 = span(box[0], box[2], size[0], width)
    y1, y2 = span(box[1], box[3], size[1], height)
    return x1, y1, x2, y2


def _keep_size(front: Box, previous: Box, shape: tuple[int, ...]) -> Box:
    """前の側の塊 front を先端に、直前の枠 previous の大きさ以上の枠を、元の側へ伸ばして作る。"""
    height, width = shape

    def span(lo: int, hi: int, old_lo: int, old_hi: int, limit: int) -> tuple[int, int]:
        size = max(hi - lo, old_hi - old_lo)
        if (lo + hi) < (old_lo + old_hi):
            return lo, min(limit - 1, lo + size)
        return max(0, hi - size), hi

    x1, x2 = span(front[0], front[2], previous[0], previous[2], width)
    y1, y2 = span(front[1], front[3], previous[1], previous[3], height)
    return x1, y1, x2, y2


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
