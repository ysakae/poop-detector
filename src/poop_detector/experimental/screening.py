"""判定器に渡す前のふるい分けに使えるかを確かめるため、動画ごとに犬検出の指標を求める。

記録するだけで、判定を省いたり通知を変えたりはしない。犬はかがむと検出の確信度が下がり、
監視のしきい値（dog.confidence）では見失いやすい（#127）。そこで、低い確信度まで拾える
検出器で、トレーに重なる確信度の低いコマと、それを含めた滞在時間、軌跡の特徴量を残す。
人のラベルや判定器の結果と比べ、本物の排泄を落とさない条件を探すための材料にする。
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..detection.roi import AnyROI
from ..features.dog import ScoredBox, overlaps_roi
from ..features.tray_tracking import TrayTracker
from ..frames import Frame, Shape
from .circling import (
    MAX_GAP_SECONDS,
    SAMPLE_SECONDS,
    Sample,
    _single_dog_position,
    sampled_frames,
)
from .trajectory import trajectory_features

if TYPE_CHECKING:
    from ..video.clip_screenings import ScreeningStore
    from ..video.clips import ClipRow

# 指標の求め方の版。求め方を変えたら上げ、古い版の記録と混ぜて比べないようにする。
SCREENING_VERSION = 1
# 指標を求めるときの検出のしきい値。かがんだ犬の確信度（0.2〜0.4。#127）まで拾う。
WEAK_CONFIDENCE = 0.2

ScoredPredict = Callable[[Frame], list[ScoredBox]]
# 1/3秒ごとの観測。位置合わせに失敗したコマは None。
Observation = tuple[float, Sequence[ScoredBox] | None]

__all__ = [
    "SAMPLE_SECONDS",
    "SCREENING_VERSION",
    "WEAK_CONFIDENCE",
    "ClipScreener",
    "Screening",
    "screen_clip",
]


@dataclass(frozen=True)
class Screening:
    """1本の動画のふるい分けの指標。滞在と移動は秒、距離は犬の大きさを単位にする。"""

    sampled: int  # 調べたコマ数
    tray_frames: int  # しきい値以上の犬がトレーに重なったコマ数
    weak_tray_frames: int  # しきい値未満の犬だけがトレーに重なったコマ数（かがんだ合図の候補）
    tray_seconds: (
        float  # しきい値以上の犬が続けてトレーに重なった最長の時間（1秒までの途切れはつなぐ）
    )
    weak_tray_seconds: float  # 確信度の低い犬も含めた、同じ最長の時間
    unaligned: int  # トレーの位置合わせに失敗したコマ数
    located: int  # トレー付近の犬1頭の位置が分かったコマ数
    net_turning: float  # 向きの変化の合計の大きさ（ラジアン）
    path_length: float  # 見失った間を除いた移動距離
    stop_seconds: float  # 止まっていた時間


def _longest_stay(stamps: Sequence[float], flags: Sequence[bool]) -> float:
    """True のコマが続いた最長の時間（秒）。MAX_GAP_SECONDS までの途切れはつなぐ。

    検出は1コマだけ外れることが多く、そのたびに滞在を切ると短く見積もってしまう。
    """
    longest = 0.0
    start = last = None
    for stamp, flag in zip(stamps, flags, strict=True):
        if not flag:
            continue
        if last is None or stamp - last > MAX_GAP_SECONDS + 1e-6:
            start = stamp
        last = stamp
        longest = max(longest, last - (start or 0.0) + SAMPLE_SECONDS)
    return longest


def summarize(
    observations: Iterable[Observation], roi: AnyROI, shape: Shape, confidence: float
) -> Screening:
    """観測の列から指標を求める。confidence は監視で使う検出のしきい値。"""
    stamps: list[float] = []
    strong: list[bool] = []
    weak: list[bool] = []
    unaligned = 0
    samples: list[Sample] = []
    for stamp, boxes in observations:
        stamps.append(stamp)
        if boxes is None:
            unaligned += 1
            strong.append(False)
            weak.append(False)
            continue
        scores = [scored.score for scored in boxes if overlaps_roi(scored.box, roi, shape)]
        strong.append(any(score >= confidence for score in scores))
        weak.append(bool(scores))
        position = _single_dog_position([scored.box for scored in boxes], roi, shape)
        if position is not None:
            samples.append((stamp, *position))
    features = trajectory_features(samples)
    return Screening(
        sampled=len(strong),
        tray_frames=sum(strong),
        weak_tray_frames=sum(w and not s for s, w in zip(strong, weak, strict=True)),
        tray_seconds=_longest_stay(stamps, strong),
        weak_tray_seconds=_longest_stay(stamps, weak),
        unaligned=unaligned,
        located=len(samples),
        net_turning=features["net_turning"],
        path_length=features["path_length"],
        stop_seconds=features["stop_seconds"],
    )


def screen_clip(
    path: str | Path,
    predict: ScoredPredict,
    roi: AnyROI,
    confidence: float,
    *,
    tray_tracker: TrayTracker | None = None,
) -> Screening:
    """動画を1/3秒ごとに調べて指標を求める。predict は WEAK_CONFIDENCE 以上の枠を返すこと。"""
    observations: list[Observation] = []
    # 位置合わせに失敗したコマには枠がないので、形は枠のあるコマのものだけを使う。
    shape: Shape = (1, 1, 3)
    for stamp, frame in sampled_frames(path, tray_tracker=tray_tracker):
        if frame is None:
            observations.append((stamp, None))
            continue
        shape = frame.shape
        observations.append((stamp, predict(frame)))
    return summarize(observations, roi, shape, confidence)


class ClipScreener:
    """動画の指標を求めて記録する。判定の前に呼ぶが、判定の流れは変えない。

    OpenCV のネットはスレッド間で共有しないので、検出器は最初に呼ばれたスレッドで作る。
    scene は動画の行から、録画時の監視範囲とトレー追従を返す。
    """

    def __init__(
        self,
        store: "ScreeningStore",
        scene: Callable[["ClipRow"], tuple[AnyROI, TrayTracker | None]],
        model_dir: Path,
        confidence: float,
    ) -> None:
        self.store, self.scene = store, scene
        self.model_dir, self.confidence = model_dir, confidence
        self.predict: ScoredPredict | None = None

    def __call__(self, path: Path, row: "ClipRow", *, again: bool = False) -> Screening | None:
        """指標を求めて保存する。いまの版で記録済みなら、again のときだけ求め直す。"""
        saved = self.store.get(row["id"])
        if saved is not None and saved["version"] == SCREENING_VERSION and not again:
            return None
        if self.predict is None:
            from ..features import dog

            self.predict = dog.DogDetector(self.model_dir, WEAK_CONFIDENCE).predict_scored
        roi, tracker = self.scene(row)
        result = screen_clip(path, self.predict, roi, self.confidence, tray_tracker=tracker)
        if not self.store.save(row["id"], result):
            raise OSError("動画の状態が変わりました")
        return result
