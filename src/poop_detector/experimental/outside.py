"""トレー外の排泄候補（実験的）。犬がトレーの外で立ち止まった場面を見つけて録画する。"""

from collections.abc import Sequence

import cv2
import numpy as np

from ..detection.roi import AnyROI, outline
from ..features.dog import Box
from ..frames import Shape


class OutsideWatch:
    """トレー外で立ち止まった場面を、滞在ごとに一度録画する。"""

    def __init__(self, tray: AnyROI, scope: AnyROI, exit_seconds: float) -> None:
        self.tray = tray
        self.scope = scope
        self.exit_seconds = exit_seconds
        self.reset()

    def reset(self) -> None:
        self.anchor: tuple[float, float] | None = None
        self.started_at = float("-inf")
        self.last_seen = float("-inf")
        self.recorded = False

    def update(self, boxes: Sequence[Box], shape: Shape, now: float) -> bool:
        """トレー外の範囲で2秒以上立ち止まったら、その滞在で1回だけ True を返す。"""
        tray = outline(self.tray, shape)
        scope = outline(self.scope, shape)
        candidates: list[tuple[int, tuple[float, float], int]] = []
        for x1, y1, x2, y2 in boxes:
            center = ((x1 + x2) / 2, (y1 + y2) / 2)
            if (
                cv2.pointPolygonTest(tray, center, False) < 0
                and cv2.pointPolygonTest(scope, center, False) >= 0
            ):
                candidates.append(((x2 - x1) * (y2 - y1), center, min(x2 - x1, y2 - y1)))
        if not candidates:
            if now - self.last_seen >= self.exit_seconds:
                self.reset()
            return False

        if now - self.last_seen > self.exit_seconds and self.anchor is not None:
            self.reset()
        self.last_seen = now
        _, center, size = max(candidates)
        if self.anchor is None or np.linalg.norm(np.subtract(self.anchor, center)) > max(
            5, size * 0.4
        ):
            self.anchor = center
            self.started_at = now
            self.recorded = False
        return not self.recorded and now - self.started_at >= 2
