"""犬の位置の列から、行動の分類を比べるための軌跡の特徴量を求める。"""

import math
from collections.abc import Sequence
from itertools import pairwise

import numpy as np

from .circling import MAX_GAP_SECONDS, Sample

# 犬の大きさに対してこれより小さい移動は、止まっているとみなす（CirclingDetector と同じ）。
STILL_RATIO = 0.12

# 特徴量の名前。距離と速さは犬の大きさを単位にし、画角や犬との距離の違いをならす。
FEATURES = (
    "located",  # 位置が分かったコマ数
    "observed_seconds",  # 最初と最後の位置の時間差（秒）
    "path_length",  # 見失った間を除いた移動距離
    "mean_speed",  # 見失った間を除いた平均の速さ（毎秒）
    "max_speed",  # 1歩の最大の速さ（毎秒）
    "straightness",  # 始点と終点の距離 ÷ 移動距離（1 で直進、0 に近いほど戻ってくる）
    "net_turning",  # 向きの変化の合計の大きさ（ラジアン。一周で 2π）
    "total_turning",  # 向きの変化の絶対値の合計（ラジアン）
    "stop_seconds",  # 止まっていた時間（秒）
    "spread",  # 位置の重心からの広がり（回転半径）
)


def _steps(samples: Sequence[Sample]) -> list[tuple[float, float, float, float]]:
    """見失った間をまたがない1歩ごとの (時間, dx, dy, 犬の大きさ) を返す。"""
    steps = []
    for (t0, x0, y0, s0), (t1, x1, y1, s1) in pairwise(samples):
        elapsed = t1 - t0
        if 0 < elapsed <= MAX_GAP_SECONDS:
            steps.append((elapsed, x1 - x0, y1 - y0, (s0 + s1) / 2))
    return steps


def _turning(moves: list[tuple[float, float]]) -> tuple[float, float]:
    """続く移動の向きの変化から、(合計の大きさ, 絶対値の合計) を返す。"""
    turns = [
        math.atan2(ax * by - ay * bx, ax * bx + ay * by) for (ax, ay), (bx, by) in pairwise(moves)
    ]
    return abs(sum(turns)), sum(abs(turn) for turn in turns)


def trajectory_features(samples: Sequence[Sample]) -> dict[str, float]:
    """位置の列から FEATURES の値を求める。位置が2件未満なら、数以外は 0 にする。"""
    features = dict.fromkeys(FEATURES, 0.0)
    features["located"] = float(len(samples))
    if len(samples) < 2:
        return features
    size = float(np.median([sample[3] for sample in samples])) or 1.0
    steps = _steps(samples)
    moving = [(dx, dy) for _, dx, dy, s in steps if math.hypot(dx, dy) >= STILL_RATIO * s]
    distances = [math.hypot(dx, dy) / size for _, dx, dy, _ in steps]
    tracked = sum(elapsed for elapsed, *_ in steps)
    path_length = sum(distances)
    points = np.asarray([sample[1:3] for sample in samples], dtype=float)
    net = float(np.linalg.norm(points[-1] - points[0])) / size
    net_turning, total_turning = _turning(moving)
    features.update(
        observed_seconds=samples[-1][0] - samples[0][0],
        path_length=path_length,
        mean_speed=path_length / tracked if tracked else 0.0,
        max_speed=max((d / step[0] for d, step in zip(distances, steps, strict=True)), default=0.0),
        straightness=min(1.0, net / path_length) if path_length else 0.0,
        net_turning=net_turning,
        total_turning=total_turning,
        stop_seconds=sum(
            elapsed for elapsed, dx, dy, s in steps if math.hypot(dx, dy) < STILL_RATIO * s
        ),
        spread=float(np.sqrt(np.mean(np.sum((points - points.mean(axis=0)) ** 2, axis=1)))) / size,
    )
    return features
