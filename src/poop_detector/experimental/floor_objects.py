"""サークルの床に新しく現れて動かない物（異物・嘔吐の跡など）の候補を見つける（実験的）。

床面（行動マップの登録）の中で、犬のいない場面から学んだ背景と比べ、新しく現れて
PERSIST_SECONDS 動かない小さな塊を候補にする。次のものは数えない。

- トレーの上（トレーの中は差分検知が受け持つ）と、床面の外
- 犬が直前までいた場所（DOG_GRACE_SECONDS の間。犬そのものの写り残りかもしれない）
- 犬ほどの大きさの塊（MAX_AREA_RATIO を超える。寝床の移動や、検出されずに寝ていた犬の跡）
- 床面の広い範囲が変わった場面（照明の変化。背景を学び直す）

一度知らせた場所は背景に取り込み、片付けたあとに戻っても知らせ直さない。
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np
from numpy.typing import NDArray

from ..detection.monitoring import MonitorExtension
from ..detection.roi import AnyROI, full_mask
from ..features.dog import Box, Predict
from ..frames import Frame
from ..records.events import utcnow
from ..records.floor_objects import FloorObjects

logger = logging.getLogger(__name__)

# 映像を調べる間隔（秒）。床の物はゆっくり確かめればよい。
SAMPLE_SECONDS = 5.0
# 候補が同じ場所に続いたら知らせる時間（秒）。
PERSIST_SECONDS = 60.0
# 犬の枠が重なった場所を数えない時間（秒）。
DOG_GRACE_SECONDS = 30.0
# 犬の枠を広げる割合（体の端や影を含める）。
DOG_MARGIN = 0.2
# 処理する映像の幅（画素）。
WIDTH = 320
GRAY_THRESHOLD = 30
COLOR_THRESHOLD = 20
# 床面に占める塊の面積の割合の下限と上限。
MIN_AREA_RATIO = 0.0005
MAX_AREA_RATIO = 0.05
# 床面のこの割合より広く変わったら、照明の変化として背景を学び直す。
UNSTABLE_RATIO = 0.3
# 犬のいない場面で背景を学ぶ速さ。
LEARN_RATE = 0.05
# 前の場面の候補と同じ塊とみなす重なり（IoU）。
MATCH_IOU = 0.3
# 候補が見えなくなってから捨てるまでの見逃しの回数。
MISSES = 2


@dataclass(frozen=True)
class FloorCandidate:
    """床に新しく現れた物の候補。box は元の映像の画素座標、area_ratio は床面に占める割合。"""

    box: Box
    area_ratio: float


@dataclass
class _Track:
    box: Box
    first_seen: float
    last_seen: float


def _iou(a: Box, b: Box) -> float:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    overlap = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap
    return overlap / union if union else 0.0


@dataclass
class _Scene:
    """処理する映像の大きさごとの状態。映像の大きさが変わったら作り直す。"""

    scale: float
    # 調べる範囲（床面からトレーを除いた場所）。
    region: NDArray[np.bool_]
    # 画素ごとに、犬の枠が最後に重なった時刻。
    dog_seen: NDArray[np.float64]
    background: NDArray[np.float32] | None = None


class FloorObjectWatch:
    """床の背景を学び、新しく現れて動かない物を候補として返す。"""

    def __init__(self, floor: AnyROI, tray: AnyROI | None) -> None:
        self.floor, self.tray = floor, tray
        self.last_sample = float("-inf")
        self.reported: list[Box] = []
        self.reset()

    def reset(self) -> None:
        """接続の途切れなどで、背景と続いていた候補を捨てる（画角が変わったかもしれない）。"""
        self.scene: _Scene | None = None
        self.tracks: list[_Track] = []

    def update(self, frame: Frame, boxes: Sequence[Box], now: float) -> list[FloorCandidate]:
        """SAMPLE_SECONDS ごとに映像を調べ、PERSIST_SECONDS 続いた新しい候補を返す。"""
        if now - self.last_sample < SAMPLE_SECONDS:
            return []
        self.last_sample = now
        scene = self._scene_for(frame)
        current = _lab(frame, scene.scale)
        _mark_dogs(scene, boxes, now)
        background = scene.background
        if background is None:
            if not boxes:
                scene.background = current
            return []
        changed = _changed(scene, background, current)
        if np.count_nonzero(changed) > UNSTABLE_RATIO * np.count_nonzero(scene.region):
            logger.info("床の映像が広く変わったので、背景を学び直します")
            scene.background, self.tracks = current, []
            return []
        found = self._candidates(scene, background, changed, current, now)
        if not boxes:
            # 変わった場所は学ばない（新しい物を、知らせる前に背景へ溶かさない）。
            learn = ~changed
            background[learn] += LEARN_RATE * (current[learn] - background[learn])
        return found

    def _scene_for(self, frame: Frame) -> _Scene:
        height, width = frame.shape[:2]
        scale = min(1.0, WIDTH / width)
        shape = (round(height * scale), round(width * scale))
        if self.scene is None or self.scene.region.shape != shape or self.scene.scale != scale:
            region = full_mask(self.floor, shape) > 0
            if self.tray is not None:
                region &= full_mask(self.tray, shape) == 0
            self.scene = _Scene(scale, region, np.full(shape, -np.inf))
            self.tracks = []
        return self.scene

    def _candidates(
        self,
        scene: _Scene,
        background: NDArray[np.float32],
        changed: NDArray[np.bool_],
        current: NDArray[np.float32],
        now: float,
    ) -> list[FloorCandidate]:
        usable = changed & (now - scene.dog_seen >= DOG_GRACE_SECONDS)
        found: list[FloorCandidate] = []
        seen: list[_Track] = []
        done: list[_Track] = []
        for box, ratio in _blobs(usable, int(np.count_nonzero(scene.region))):
            if any(_iou(box, old) >= MATCH_IOU for old in self.reported):
                # 知らせた物を片付けた跡など。背景に取り込んで、知らせ直さない。
                _absorb(background, box, current)
                continue
            track = next((t for t in self.tracks if _iou(box, t.box) >= MATCH_IOU), None)
            if track is None:
                track = _Track(box, now, now)
            track.box, track.last_seen = box, now
            if now - track.first_seen < PERSIST_SECONDS:
                seen.append(track)
                continue
            found.append(FloorCandidate(_original(box, scene.scale), ratio))
            self.reported.append(box)
            _absorb(background, box, current)
            done.append(track)
        # 見えなくなった候補は、MISSES 回の見逃しまで覚えておく（一時的な遮りに耐える）。
        recent = now - MISSES * SAMPLE_SECONDS
        kept = [
            t
            for t in self.tracks
            if t.last_seen >= recent and not any(t is other for other in (*seen, *done))
        ]
        self.tracks = seen + kept
        return found


def _lab(frame: Frame, scale: float) -> NDArray[np.float32]:
    height, width = frame.shape[:2]
    small = cv2.resize(frame, (round(width * scale), round(height * scale)))
    blurred = cv2.GaussianBlur(small, (5, 5), 0)
    return cv2.cvtColor(blurred, cv2.COLOR_BGR2LAB).astype(np.float32)


def _mark_dogs(scene: _Scene, boxes: Sequence[Box], now: float) -> None:
    height, width = scene.region.shape
    for x1, y1, x2, y2 in boxes:
        margin_x, margin_y = (x2 - x1) * DOG_MARGIN, (y2 - y1) * DOG_MARGIN
        left = max(0, int((x1 - margin_x) * scene.scale))
        top = max(0, int((y1 - margin_y) * scene.scale))
        right = min(width, int(np.ceil((x2 + margin_x) * scene.scale)) + 1)
        bottom = min(height, int(np.ceil((y2 + margin_y) * scene.scale)) + 1)
        scene.dog_seen[top:bottom, left:right] = now


def _changed(
    scene: _Scene, background: NDArray[np.float32], current: NDArray[np.float32]
) -> NDArray[np.bool_]:
    difference = np.abs(current - background)
    changed = (difference[:, :, 0] > GRAY_THRESHOLD) | (
        np.max(difference[:, :, 1:], axis=2) > COLOR_THRESHOLD
    )
    changed &= scene.region
    opened = cv2.morphologyEx(changed.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return opened > 0


def _blobs(usable: NDArray[np.bool_], floor_area: int) -> list[tuple[Box, float]]:
    """変わった画素の塊のうち、面積が床の物らしいものの枠と、床面に占める割合。"""
    count, _, stats, _ = cv2.connectedComponentsWithStats(usable.astype(np.uint8))
    blobs: list[tuple[Box, float]] = []
    for index in range(1, count):
        x, y, w, h, area = (int(value) for value in stats[index])
        ratio = area / floor_area
        if MIN_AREA_RATIO <= ratio <= MAX_AREA_RATIO:
            blobs.append(((x, y, x + w, y + h), ratio))
    return blobs


def _absorb(background: NDArray[np.float32], box: Box, current: NDArray[np.float32]) -> None:
    x1, y1, x2, y2 = box
    background[y1:y2, x1:x2] = current[y1:y2, x1:x2]


def _original(box: Box, scale: float) -> Box:
    x1, y1, x2, y2 = box
    return (round(x1 / scale), round(y1 / scale), round(x2 / scale), round(y2 / scale))


class FloorNotifier(Protocol):
    """床の候補を知らせる通知先。"""

    def send_floor_object(self, object_id: str, detected_at: str) -> bool: ...


class FloorObjectExtension(MonitorExtension):
    """位置合わせ前の映像で床の候補を探し、記録して知らせる（features.floor_objects）。"""

    def __init__(
        self,
        watch: FloorObjectWatch,
        predict: Predict,
        objects: FloorObjects,
        notifier: FloorNotifier,
        *,
        notify: bool = True,
    ) -> None:
        self.watch, self.predict, self.objects = watch, predict, objects
        self.notifier, self.notify = notifier, notify

    def on_camera_frame(self, raw: Frame | None, disconnected: bool, tick: float) -> None:
        # トレー追従に失敗しても床面の映像は使えるので、接続の途切れだけで区切る。
        if disconnected:
            self.watch.reset()
        if raw is None or tick - self.watch.last_sample < SAMPLE_SECONDS:
            return
        try:
            boxes = self.predict(raw)
        except (cv2.error, OSError, ValueError):
            logger.warning("床の候補のための犬検出に失敗しました")
            # 犬がいるかもしれないので、この場面では調べない。
            self.watch.last_sample = tick
            return
        for candidate in self.watch.update(raw, boxes, tick):
            self._record(raw, candidate)

    def _record(self, raw: Frame, candidate: FloorCandidate) -> None:
        detected_at = utcnow()
        object_id = self.objects.add(raw, candidate.box, candidate.area_ratio)
        logger.info("床に新しく現れた物の候補を記録しました: %s", object_id)
        if self.notify and self.notifier.send_floor_object(object_id, detected_at):
            self.objects.mark_notified(object_id)
