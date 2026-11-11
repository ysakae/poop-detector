"""登録したトレーの縁を照合し、基準画像と同じ座標へ映像を戻す。"""

from pathlib import Path
from typing import Any, Self

import cv2
import numpy as np
from numpy.typing import NDArray

from ..detection.roi import ROI, AnyROI
from ..frames import Frame, Mask

# 縮小した映像上の 2×3 のアフィン変換。
Transform = NDArray[Any]

# 追従に使う特徴点の最小の数。縁の形の照合の内部の値で、設定にはしない。
MIN_MATCHES = 6
FLOW_CRITERIA = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.02)


def _optical_flow(previous: Frame, following: Frame, points: NDArray[Any]) -> tuple[Any, Any, Any]:
    """points が following のどこへ動いたかを追跡し、(位置, 成否, 誤差) を返す。"""
    # OpenCV は nextPts に None を受け付けるが、型スタブには書かれていない。
    return cv2.calcOpticalFlowPyrLK(  # type: ignore[call-overload,no-any-return]
        previous,
        following,
        points,
        None,
        winSize=(21, 21),
        maxLevel=3,
        criteria=FLOW_CRITERIA,
    )


class TrayReferenceError(ValueError):
    """トレー追従用の基準画像がないか、使えない。"""


class TrayTracker:
    """トレーの縁を目印に、カメラのずれを登録時の位置へ戻す。"""

    @classmethod
    def from_baseline(cls, path: str | Path, baseline: Frame, roi: AnyROI) -> Self:
        """保存したトレー追従用の画像を読み、基準画像と同じ場面か確かめてから作る。"""
        reference = cv2.imread(str(path))
        if reference is None or not np.array_equal(roi.crop(reference), baseline):
            raise TrayReferenceError(
                "トレー追従用の基準画像がありません。"
                "空のトイレで capture-baseline --replace --track-tray を実行してください"
            )
        return cls(reference, roi)

    def __init__(self, reference: Frame, roi: AnyROI) -> None:
        height, width = reference.shape[:2]
        self.output_size = (width, height)
        scale = min(1, 640 / max(width, height))
        self.size = (max(1, round(width * scale)), max(1, round(height * scale)))
        self.reference = self._gray(reference)
        w, h = self.size
        if isinstance(roi, ROI):
            x, y, rw, rh = roi.pixels(w, h)
            self.boundary: NDArray[np.float32] = np.array(
                [[x, y], [x + rw - 1, y], [x + rw - 1, y + rh - 1], [x, y + rh - 1]],
                dtype=np.float32,
            )
        else:
            self.boundary = roi.pixels(w, h)
        self.min_span = 0.15 * cv2.contourArea(self.boundary)
        interior = np.zeros((h, w), np.uint8)
        cv2.fillPoly(interior, [np.rint(self.boundary).astype(np.int32)], 255)
        points = self._rim_points(self.reference, interior)
        if points is None:
            raise TrayReferenceError(
                "トレーの縁を十分に確認できません。ROIと基準画像を確認してください"
            )
        self.points = points
        self.detail_reference = self._local_detail(self.reference)
        self.detail_points = self._rim_points(self.detail_reference, interior)
        self.edge_reference = self._rim_edges(self.reference)
        self.edge_points = self._rim_points(self.edge_reference, interior)

    def _rim_points(self, reference: Frame, interior: Mask) -> NDArray[Any] | None:
        """トレーの縁の帯から、追跡に使う特徴点を探す。十分に見つからなければ None。"""
        short_side = min(np.ptp(self.boundary, axis=0))
        # 模様の少ない太い縁では、内側の角まで探索範囲を広げる。
        for fraction in (0.075, 0.14):
            band_width = max(3, round(fraction * short_side))
            inner = cv2.erode(
                interior,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * band_width + 1,) * 2),
                borderType=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            points = cv2.goodFeaturesToTrack(
                reference, 64, 0.005, 10, mask=interior - inner, blockSize=5
            )
            if (
                points is not None
                and len(points) >= MIN_MATCHES
                and cv2.contourArea(cv2.convexHull(points)) >= self.min_span
            ):
                result: NDArray[Any] = points
                return result
        return None

    def _gray(self, frame: Frame) -> Frame:
        return cv2.cvtColor(
            cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY
        )

    @staticmethod
    def _local_detail(gray: Frame) -> Frame:
        # 緩やかな明るさの違いを差し引き、縁の形を照合する。
        return cv2.addWeighted(gray, 1, cv2.GaussianBlur(gray, (0, 0), 5), -1, 128)

    @classmethod
    def _rim_edges(cls, gray: Frame) -> Frame:
        # 赤外線で明暗が逆転しても、符号を除いた輪郭の形は残る。
        smoothed = cv2.GaussianBlur(gray, (0, 0), 1.5)
        edges = cv2.convertScaleAbs(cv2.Laplacian(smoothed, cv2.CV_32F, ksize=3), alpha=0.5)
        return cls._local_detail(edges)

    @staticmethod
    def _monochrome(frame: Frame) -> bool:
        """赤外線の白黒映像か（色の差がほとんどないか）。"""
        blue, green, red = cv2.split(frame[::16, ::16])
        return bool(cv2.absdiff(blue, green).mean() < 1 and cv2.absdiff(red, green).mean() < 1)

    def align(self, frame: Frame, *, source_mask: Mask | None = None) -> Frame | None:
        """映像を基準画像の位置へ戻す。トレーの縁を確かめられなければ None。"""
        height, width = frame.shape[:2]
        ref_width, ref_height = self.output_size
        if abs((width / height) / (ref_width / ref_height) - 1) > 0.01:
            return None
        gray = self._gray(frame)
        transform = None
        if self.edge_points is not None and self._monochrome(frame):
            transform = self._estimate_transform(
                self.edge_reference, self._rim_edges(gray), self.edge_points
            )
        if transform is None:
            transform = self._estimate_transform(self.reference, gray, self.points)
        if transform is None and self.detail_points is not None:
            transform = self._estimate_transform(
                self.detail_reference, self._local_detail(gray), self.detail_points
            )
        if transform is None:
            return None
        if source_mask is not None:
            frame = cv2.bitwise_and(frame, frame, mask=source_mask)
        return self._warp(frame, transform)

    def _estimate_transform(
        self, reference: Frame, gray: Frame, points: NDArray[Any]
    ) -> Transform | None:
        forward, status, error = _optical_flow(reference, gray, points)
        if forward is None:
            return None
        matched = (status.ravel() == 1) & (error.ravel() < 24)
        original, moved = points[matched], forward[matched]
        if len(moved) < MIN_MATCHES:
            return None
        backward, status, _ = _optical_flow(gray, reference, moved)
        if backward is None:
            return None
        matched = (status.ravel() == 1) & (
            np.linalg.norm(original - backward, axis=2).ravel() < 1.5
        )
        original, moved = original[matched], moved[matched]
        if len(moved) < MIN_MATCHES:
            return None
        transform, inliers = cv2.estimateAffine2D(original, moved, ransacReprojThreshold=2)
        if transform is None or not np.isfinite(transform).all():
            return None
        matched = inliers.ravel().astype(bool)
        if (
            matched.sum() < max(MIN_MATCHES, 0.55 * len(moved))
            or cv2.contourArea(cv2.convexHull(original[matched])) < self.min_span
        ):
            return None
        result: Transform = transform
        return result

    def _warp(self, frame: Frame, transform: Transform) -> Frame | None:
        scales = np.linalg.svd(transform[:, :2], compute_uv=False)
        if scales.min() < 0.85 or scales.max() > 1.15 or np.linalg.det(transform[:, :2]) <= 0:
            return None
        boundary = cv2.transform(self.boundary[None, :, :], transform)[0]
        w, h = self.size
        displacement = np.linalg.norm(boundary - self.boundary, axis=1).max()
        if (
            (boundary < -1).any()
            or (boundary > [w, h]).any()
            or displacement > 0.12 * max(self.size)
        ):
            return None
        height, width = frame.shape[:2]
        if displacement < 0.35 and (width, height) == self.output_size:
            return frame
        ref_width, ref_height = self.output_size
        # 縮小映像上の変換を入力と基準画像の解像度へ戻す。
        inverse_map = (
            np.diag([width / w, height / h, 1])
            @ np.vstack((transform, [0, 0, 1]))
            @ np.diag([w / ref_width, h / ref_height, 1])
        )
        return cv2.warpAffine(
            frame,
            inverse_map[:2],
            self.output_size,
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_REPLICATE,
        )
