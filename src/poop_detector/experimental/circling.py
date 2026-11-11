"""保存映像の犬の短い軌跡から、トレー付近の旋回候補を抽出する。"""

import math
from collections import deque
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from ..detection.roi import AnyROI, outline
from ..features.dog import Predict
from ..features.tray_tracking import TrayTracker

# 旋回分析は保存済みの動画に対する実験的な分析で、通知や監視には使わない。
# 値は人工の軌跡と少数の動画で決めたもので、設定にはしない。
SAMPLE_SECONDS = 1 / 3  # 犬の位置を調べる間隔（秒）
WINDOW_SECONDS = 8  # 旋回を探す軌跡の長さ（秒）
MAX_GAP_SECONDS = 1  # これより長く見失ったら軌跡を切る（秒）
MIN_POINTS = 7  # 旋回とみなすのに必要な位置の数


class CirclingDetector:
    """犬の位置の軌跡から、その場で回る動き（排泄の前兆候補）を見つける。"""

    def __init__(self) -> None:
        # (時刻, x, y, 犬の大きさ)
        self.path: deque[tuple[float, float, float, float]] = deque()
        self.last_seen: float | None = None

    def update(self, stamp: float, x: float, y: float, size: float) -> tuple[float, float] | None:
        """位置を1つ足し、旋回を見つけたらその開始と終了の秒数を返す。"""
        if self.last_seen is not None and stamp - self.last_seen > MAX_GAP_SECONDS:
            self.path.clear()
        self.last_seen = stamp
        while self.path and stamp - self.path[0][0] > WINDOW_SECONDS:
            self.path.popleft()
        if self.path and math.dist((x, y), self.path[-1][1:3]) < 0.12 * size:
            return None
        self.path.append((stamp, x, y, size))
        if len(self.path) < MIN_POINTS:
            return None
        samples = np.asarray(self.path)
        for start in range(len(samples) - MIN_POINTS + 1):
            if is_loop(samples[start:]):
                self.path.clear()
                return round(float(samples[start, 0]), 2), round(stamp, 2)
        return None


def is_loop(samples: NDArray[Any]) -> bool:
    """軌跡が、ほぼ一周してもとの位置へ戻る動きか。"""
    points = samples[:, 1:3]
    size = np.median(samples[:, 3])
    if samples[-1, 0] - samples[0, 0] < 1.5:
        return False
    centered = points - points.mean(axis=0)
    radii = np.linalg.norm(centered, axis=1)
    if radii.min() < 0.25 * size or radii.max() > 1.6 * size:
        return False
    if np.linalg.norm(points[-1] - points[0]) > 0.9 * np.median(radii):
        return False
    left, right = centered[:-1], centered[1:]
    cross = left[:, 0] * right[:, 1] - left[:, 1] * right[:, 0]
    dot = np.sum(left * right, axis=1)
    turns = np.arctan2(cross, dot)
    rotation = abs(turns.sum())
    return bool(
        np.max(np.abs(turns)) < math.pi / 2
        and rotation >= math.radians(270)
        and rotation >= 0.8 * np.abs(turns).sum()
    )


def inspect_circling(
    path: str | Path,
    predict: Predict,
    roi: AnyROI,
    *,
    tray_tracker: TrayTracker | None = None,
) -> tuple[list[tuple[float, float]], int, int]:
    """動画を1/3秒ごとに調べ、(旋回候補, 調べたコマ数, 犬の位置が分かったコマ数) を返す。"""
    capture = cv2.VideoCapture(str(path))
    detector = CirclingDetector()
    candidates: list[tuple[float, float]] = []
    sampled = located = 0
    next_sample = 0.0
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if not math.isfinite(fps) or fps <= 0 or count < 2:
            raise ValueError("動画からフレームを取得できません")
        for index in range(count):
            if not capture.grab():
                raise ValueError("動画の一部を読み込めません")
            stamp = index / fps
            if stamp + 1e-6 < next_sample:
                continue
            next_sample = (math.floor((stamp + 1e-6) / SAMPLE_SECONDS) + 1) * SAMPLE_SECONDS
            ok, frame = capture.retrieve()
            if not ok:
                raise ValueError("動画の一部を読み込めません")
            sampled += 1
            if tray_tracker is not None:
                aligned = tray_tracker.align(frame)
                if aligned is None:
                    continue
                frame = aligned
            boundary = outline(roi, frame.shape)
            positions: list[tuple[float, float, float]] = []
            for x1, y1, x2, y2 in predict(frame):
                x, y = (x1 + x2) / 2, (y1 + y2) / 2
                size = math.sqrt((x2 - x1) * (y2 - y1))
                if cv2.pointPolygonTest(boundary, (x, y), True) >= -0.25 * size:
                    positions.append((x, y, size))
            if len(positions) == 1:
                located += 1
                candidate = detector.update(stamp, *positions[0])
                if candidate is not None:
                    candidates.append(candidate)
    finally:
        capture.release()
    return candidates, sampled, located
