"""解像度に依存しない監視領域。"""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Self, TypeAlias

import cv2
import numpy as np
from numpy.typing import NDArray

from .frames import Frame, Mask, Shape

Point: TypeAlias = Sequence[float]


@dataclass(frozen=True)
class ROI:
    """矩形の監視範囲。座標は画像の幅と高さに対する割合で持つ。"""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
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
    def from_pixels(cls, rect: Sequence[float], width: int, height: int) -> Self:
        x, y, w, h = rect
        return cls(x / width, y / height, w / width, h / height)

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            round(self.x * width),
            round(self.y * height),
            max(1, round(self.width * width)),
            max(1, round(self.height * height)),
        )

    def crop(self, frame: Frame) -> Frame:
        x, y, w, h = self.pixels(frame.shape[1], frame.shape[0])
        return frame[y : y + h, x : x + w]

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)))

    @classmethod
    def load(cls, path: str | Path) -> "AnyROI":
        """保存した JSON の内容から、矩形・四隅・多角形のどの ROI かを選んで読み込む。"""
        data = json.loads(Path(path).read_text())
        if data.get("mode") == "mask":
            return MaskedROI(**data)
        return PolygonROI(**data) if "points" in data else cls(**data)


@dataclass(frozen=True)
class PolygonROI:
    """四隅を周回順に保存し、トイレ面を長方形へ射影補正する。"""

    points: list[list[float]]

    def __post_init__(self) -> None:
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
    def from_pixels(cls, points: Sequence[Point], width: int, height: int) -> Self:
        if width < 2 or height < 2:
            raise ValueError("画像の幅と高さが不足しています")
        normalized = np.asarray(points, dtype=np.float32) / [width - 1, height - 1]
        return cls(normalized.tolist())

    def pixels(self, width: int, height: int) -> NDArray[np.float32]:
        return np.asarray(self.points, dtype=np.float32) * np.array(
            [width - 1, height - 1], dtype=np.float32
        )

    def crop(self, frame: Frame) -> Frame:
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

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)))


def _segments_intersect(a: NDArray[Any], b: NDArray[Any], c: NDArray[Any], d: NDArray[Any]) -> bool:
    """端点の接触や重なりも交差として扱う。"""

    def cross(p: NDArray[Any], q: NDArray[Any], r: NDArray[Any]) -> float:
        return float((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]))

    def on_segment(p: NDArray[Any], q: NDArray[Any], r: NDArray[Any]) -> bool:
        return bool(np.all(r >= np.minimum(p, q) - 1e-8) and np.all(r <= np.maximum(p, q) + 1e-8))

    ab_c, ab_d, cd_a, cd_b = cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)
    if ab_c * ab_d < 0 and cd_a * cd_b < 0:
        return True
    return any(
        abs(value) <= 1e-8 and on_segment(p, q, r)
        for value, p, q, r in [(ab_c, a, b, c), (ab_d, a, b, d), (cd_a, c, d, a), (cd_b, c, d, b)]
    )


@dataclass(frozen=True)
class MaskedROI(PolygonROI):
    """3点以上の単純多角形。射影補正せず、外側を監視から除外する。"""

    mode: str = "mask"

    def __post_init__(self) -> None:
        points = np.asarray(self.points, dtype=np.float32)
        if (
            points.ndim != 2
            or points.shape[1] != 2
            or len(points) < 3
            or not np.isfinite(points).all()
            or self.mode != "mask"
        ):
            raise ValueError("3点以上の正しい頂点を指定してください")
        if np.any(points < 0) or np.any(points > 1):
            raise ValueError("頂点は画像の内側を指定してください")
        if len(np.unique(points, axis=0)) != len(points) or abs(cv2.contourArea(points)) < 1e-6:
            raise ValueError("重複する頂点や面積のない多角形は指定できません")
        count = len(points)
        for i in range(count):
            for j in range(i + 1, count):
                if j == i + 1 or (i == 0 and j == count - 1):
                    continue
                if _segments_intersect(
                    points[i], points[(i + 1) % count], points[j], points[(j + 1) % count]
                ):
                    raise ValueError("辺が交差しない順に頂点を指定してください")
        object.__setattr__(self, "points", points.tolist())

    def _bounds(self, shape: Shape) -> tuple[NDArray[np.int32], tuple[int, int, int, int]]:
        points = np.rint(self.pixels(shape[1], shape[0])).astype(np.int32)
        x, y, width, height = cv2.boundingRect(points)
        return points, (x, y, width, height)

    def mask(self, shape: Shape) -> Mask:
        points, (x, y, width, height) = self._bounds(shape)
        mask = np.zeros((height, width), dtype=np.uint8)
        cv2.fillPoly(mask, [points - [x, y]], 255)
        return mask

    def crop(self, frame: Frame) -> Frame:
        _, (x, y, width, height) = self._bounds(frame.shape)
        crop = frame[y : y + height, x : x + width]
        return cv2.bitwise_and(crop, crop, mask=self.mask(frame.shape))


AnyROI: TypeAlias = ROI | PolygonROI
"""ROI.load が返しうる型。MaskedROI は PolygonROI の派生。"""


def outline(roi: AnyROI, shape: Shape) -> NDArray[np.int32]:
    """ROI の外周を画素座標の頂点列で返す。"""
    height, width = shape[:2]
    if isinstance(roi, ROI):
        x, y, w, h = roi.pixels(width, height)
        return np.array([[x, y], [x + w - 1, y], [x + w - 1, y + h - 1], [x, y + h - 1]], np.int32)
    return np.rint(roi.pixels(width, height)).astype(np.int32)


def full_mask(roi: AnyROI, shape: Shape) -> Mask:
    mask = np.zeros(shape[:2], np.uint8)
    cv2.fillPoly(mask, [outline(roi, shape)], 255)
    return mask


def select_polygon(frame: Frame, window: str = "トイレの多角形を指定") -> "MaskedROI":
    """外周に沿って3点以上をクリックし、Enterで確定する。"""
    points: list[tuple[int, int]] = []
    height, width = frame.shape[:2]

    def on_mouse(event: int, x: int, y: int, _flags: int, _param: object) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            if 0 <= x < width and 0 <= y < height:
                points.append((x, y))
        elif event == cv2.EVENT_RBUTTONDOWN and points:
            points.pop()

    # 範囲の登録は対話的な操作なので、操作方法をターミナルに表示する。
    print("外周に沿って3点以上をクリック。Enter:確定、右クリック:戻す、R:やり直し、Esc:取消")  # noqa: T201
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    scale = min(1, 1280 / width, 800 / height)
    cv2.resizeWindow(window, round(width * scale), round(height * scale))
    cv2.setMouseCallback(window, on_mouse)
    try:
        while True:
            preview = frame.copy()
            if len(points) > 1:
                cv2.polylines(
                    preview, [np.array(points, dtype=np.int32)], len(points) >= 3, (0, 220, 0), 2
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
            elif key in (10, 13) and len(points) >= 3:
                try:
                    return MaskedROI.from_pixels(points, width, height)
                except ValueError:
                    print("多角形が交差・重複しているか面積がありません。Rでやり直してください")  # noqa: T201
    finally:
        cv2.destroyWindow(window)
