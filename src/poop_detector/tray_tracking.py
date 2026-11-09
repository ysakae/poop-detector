"""登録したトレーの縁を照合し、基準画像と同じ座標へ映像を戻す。"""

import cv2
import numpy as np

from .roi import ROI

MIN_MATCHES = 6
FLOW = {
    "winSize": (21, 21),
    "maxLevel": 3,
    "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.02),
}


class TrayReferenceError(ValueError):
    pass


class TrayTracker:
    @classmethod
    def from_baseline(cls, path, baseline, roi):
        reference = cv2.imread(str(path))
        if reference is None or not np.array_equal(roi.crop(reference), baseline):
            raise TrayReferenceError(
                "トレー追従用の基準画像がありません。"
                "空のトイレで capture-baseline --replace --track-tray を実行してください"
            )
        return cls(reference, roi)

    def __init__(self, reference, roi):
        height, width = reference.shape[:2]
        self.output_size = (width, height)
        scale = min(1, 640 / max(width, height))
        self.size = (max(1, round(width * scale)), max(1, round(height * scale)))
        self.reference = self._gray(reference)
        w, h = self.size
        if isinstance(roi, ROI):
            x, y, rw, rh = roi.pixels(w, h)
            self.boundary = np.float32(
                [[x, y], [x + rw - 1, y], [x + rw - 1, y + rh - 1], [x, y + rh - 1]]
            )
        else:
            self.boundary = roi.pixels(w, h)
        self.min_span = 0.15 * cv2.contourArea(self.boundary)
        interior = np.zeros((h, w), np.uint8)
        cv2.fillPoly(interior, [np.rint(self.boundary).astype(np.int32)], 255)
        self.points = self._rim_points(self.reference, interior)
        if self.points is None:
            raise TrayReferenceError(
                "トレーの縁を十分に確認できません。ROIと基準画像を確認してください"
            )
        self.detail_reference = self._local_detail(self.reference)
        self.detail_points = self._rim_points(self.detail_reference, interior)

    def _rim_points(self, reference, interior):
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
                return points
        return None

    def _gray(self, frame):
        return cv2.cvtColor(
            cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY
        )

    @staticmethod
    def _local_detail(gray):
        # 緩やかな明るさの違いを差し引き、縁の形を照合する。
        return cv2.addWeighted(gray, 1, cv2.GaussianBlur(gray, (0, 0), 5), -1, 128)

    def align(self, frame, *, source_mask=None):
        height, width = frame.shape[:2]
        ref_width, ref_height = self.output_size
        if abs((width / height) / (ref_width / ref_height) - 1) > 0.01:
            return None
        gray = self._gray(frame)
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

    def _estimate_transform(self, reference, gray, points):
        forward, status, error = cv2.calcOpticalFlowPyrLK(reference, gray, points, None, **FLOW)
        if forward is None:
            return None
        matched = (status.ravel() == 1) & (error.ravel() < 24)
        original, moved = points[matched], forward[matched]
        if len(moved) < MIN_MATCHES:
            return None
        backward, status, _ = cv2.calcOpticalFlowPyrLK(gray, reference, moved, None, **FLOW)
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
        return transform

    def _warp(self, frame, transform):
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
