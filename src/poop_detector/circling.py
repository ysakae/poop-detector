"""保存映像の犬の短い軌跡から、トレー付近の旋回候補を抽出する。"""

import math
from collections import deque

import cv2
import numpy as np

from .roi import outline

SAMPLE_SECONDS = 1 / 3
WINDOW_SECONDS = 8
MAX_GAP_SECONDS = 1
MIN_POINTS = 7


class CirclingDetector:
    """犬の位置の軌跡から、その場で回る動き（排泄の前兆候補）を見つける。"""

    def __init__(self):
        self.path = deque()
        self.last_seen = None

    def update(self, stamp, x, y, size):
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


def is_loop(samples):
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


def inspect_circling(path, predict, roi, *, tray_tracker=None):
    capture = cv2.VideoCapture(str(path))
    detector = CirclingDetector()
    candidates = []
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
                frame = tray_tracker.align(frame)
                if frame is None:
                    continue
            boundary = outline(roi, frame.shape)
            positions = []
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
