"""解像度に依存しない監視領域。"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class ROI:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self):
        if not (
            0 <= self.x < 1
            and 0 <= self.y < 1
            and self.width > 0
            and self.height > 0
            and self.x + self.width <= 1
            and self.y + self.height <= 1
        ):
            raise ValueError("ROIは画像内の空でない領域を指定してください")

    @classmethod
    def from_pixels(cls, rect, width, height):
        x, y, w, h = rect
        return cls(x / width, y / height, w / width, h / height)

    def pixels(self, width, height):
        return (
            round(self.x * width),
            round(self.y * height),
            max(1, round(self.width * width)),
            max(1, round(self.height * height)),
        )

    def crop(self, frame):
        x, y, w, h = self.pixels(frame.shape[1], frame.shape[0])
        return frame[y : y + h, x : x + w]

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)))

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text())
        return PolygonROI(**data) if "points" in data else cls(**data)


@dataclass(frozen=True)
class PolygonROI:
    """四隅を周回順に保存し、トイレ面を長方形へ射影補正する。"""

    points: list[list[float]]

    def __post_init__(self):
        points = np.asarray(self.points, dtype=np.float32)
        if points.shape != (4, 2) or not np.isfinite(points).all():
            raise ValueError("四隅を正しい座標で指定してください")
        if np.any(points < 0) or np.any(points > 1):
            raise ValueError("四隅は画像の内側を指定してください")
        if not cv2.isContourConvex(points) or abs(cv2.contourArea(points)) < 1e-6:
            raise ValueError("交差やくぼみのない四角形を周回順に指定してください")
        # JSON往復でタプル・リストの違いによる基準画像の不一致を防ぐ。
        object.__setattr__(self, "points", points.tolist())

    @classmethod
    def from_pixels(cls, points, width, height):
        if width < 2 or height < 2:
            raise ValueError("画像の幅と高さが不足しています")
        normalized = np.asarray(points, dtype=np.float32) / [width - 1, height - 1]
        return cls(normalized.tolist())

    def pixels(self, width, height):
        return np.asarray(self.points, dtype=np.float32) * np.array(
            [width - 1, height - 1], dtype=np.float32
        )

    def crop(self, frame):
        points = self.pixels(frame.shape[1], frame.shape[0])
        top_left, top_right, bottom_right, bottom_left = points
        width = max(
            2,
            round(
                max(
                    np.linalg.norm(top_right - top_left), np.linalg.norm(bottom_right - bottom_left)
                )
            )
            + 1,
        )
        height = max(
            2,
            round(
                max(
                    np.linalg.norm(bottom_left - top_left), np.linalg.norm(bottom_right - top_right)
                )
            )
            + 1,
        )
        target = np.array(
            [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32
        )
        transform = cv2.getPerspectiveTransform(points, target)
        return cv2.warpPerspective(frame, transform, (width, height))

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)))


def select_polygon(frame):
    """左上・右上・右下・左下の順にクリックし、Enterで確定する。"""
    window = "トイレの四隅を指定"
    points = []
    height, width = frame.shape[:2]

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < 4:
            if 0 <= x < width and 0 <= y < height:
                points.append((x, y))
        elif event == cv2.EVENT_RBUTTONDOWN and points:
            points.pop()

    print("左上→右上→右下→左下をクリック。Enter:確定、右クリック:戻す、R:やり直し、Esc:取消")
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    scale = min(1, 1280 / width, 800 / height)
    cv2.resizeWindow(window, round(width * scale), round(height * scale))
    cv2.setMouseCallback(window, on_mouse)
    try:
        while True:
            preview = frame.copy()
            if len(points) > 1:
                cv2.polylines(
                    preview, [np.array(points, dtype=np.int32)], len(points) == 4, (0, 220, 0), 2
                )
            for index, point in enumerate(points, 1):
                cv2.circle(preview, point, 6, (0, 220, 0), -1)
                cv2.putText(
                    preview,
                    str(index),
                    (point[0] + 8, point[1] - 8),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 220, 0),
                    2,
                )
            cv2.imshow(window, preview)
            key = cv2.waitKey(20) & 0xFF
            if key == 27 or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                raise ValueError("ROI選択を取り消しました")
            if key in (ord("r"), ord("R")):
                points.clear()
            elif key in (8, 127) and points:
                points.pop()
            elif key in (10, 13) and len(points) == 4:
                try:
                    return PolygonROI.from_pixels(points, width, height)
                except ValueError:
                    print("四角形が交差しています。Rでやり直してください")
    finally:
        cv2.destroyWindow(window)
