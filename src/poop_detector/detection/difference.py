"""画像差分は排泄物の識別ではなく変化の候補を返す。"""

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

import cv2
import numpy as np
from numpy.typing import NDArray

from ..errors import ReferenceMismatchError
from ..frames import Frame, Mask

# empty: 基準と同じ、candidate: 小さな変化の候補、occupied: 遮蔽、unstable: 広範囲の変動
ObservationKind: TypeAlias = Literal["empty", "candidate", "occupied", "unstable"]


@dataclass
class Observation:
    """1フレームの判定結果。kind は empty・candidate・occupied・unstable のいずれか。"""

    kind: ObservationKind
    score: float


class Detector:
    """空のトイレの基準画像との明るさ・色の差分から、変化の候補を判定する。"""

    def __init__(
        self,
        baseline: Frame,
        threshold: int = 30,
        min_area: int = 40,
        occupied_ratio: float = 0.18,
        color_threshold: int = 20,
    ) -> None:
        self.shape = baseline.shape
        self.baseline = self.gray(baseline)
        self.threshold = threshold
        self.min_area = min_area
        self.occupied_ratio = occupied_ratio
        self.color_threshold = color_threshold
        self.baseline_color = self.color(baseline)

    @staticmethod
    def color(frame: Frame) -> Frame:
        return cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2LAB), (5, 5), 0)[:, :, 1:]

    @staticmethod
    def gray(frame: Frame) -> Frame:
        return cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0)

    def valid_region(self, frame: Frame, valid_mask: Mask | None = None) -> NDArray[np.bool_]:
        if frame.shape != self.shape:
            raise ReferenceMismatchError(
                "基準画像と解像度が異なります。基準画像を再登録してください"
            )
        if valid_mask is None:
            valid = np.ones(frame.shape[:2], dtype=bool)
        else:
            valid = np.asarray(valid_mask) > 0
            if valid.shape != frame.shape[:2] or not np.any(valid):
                raise ValueError("監視マスクの形状または面積が不正です")
        return valid

    def difference_mask(self, frame: Frame, valid: NDArray[np.bool_]) -> Frame:
        diff = cv2.absdiff(self.gray(frame), self.baseline)
        mask = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)[1]
        if self.color_threshold:
            color_diff = cv2.absdiff(self.color(frame), self.baseline_color)
            mask[np.max(color_diff, axis=2) > self.color_threshold] = 255
        mask[~valid] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask[~valid] = 0
        return mask

    def analyze(self, frame: Frame, valid_mask: Mask | None = None) -> Observation:
        valid = self.valid_region(frame, valid_mask)
        gray = self.gray(frame)
        signed = gray.astype(np.int16) - self.baseline.astype(np.int16)
        if float(np.mean(np.abs(signed[valid]) > self.threshold)) > 0.8:
            return Observation("unstable", 0.0)
        return self.classify_mask(self.difference_mask(frame, valid), valid)

    def classify_mask(self, mask: Frame, valid: NDArray[np.bool_]) -> Observation:
        ratio = float(np.count_nonzero(mask)) / np.count_nonzero(valid)
        if ratio >= self.occupied_ratio:
            return Observation("occupied", ratio)
        contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
        candidates: list[float] = []
        height, width = mask.shape
        margin = max(1, round(min(height, width) * 0.01))
        # 斜辺やくぼみも含めた監視領域の境界からの距離を使う。
        padded = np.pad(valid.astype(np.uint8), 1)
        distance = cv2.distanceTransform(padded, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)[1:-1, 1:-1]
        boundary = valid & (distance <= margin + 1)
        for contour in contours:
            area = cv2.contourArea(contour)
            _, _, w, h = cv2.boundingRect(contour)
            # ROI外から入り込んだ体の一部は、面積が小さくても遮蔽として保留する。
            coords = contour[:, 0, :]
            if area >= self.min_area and np.any(boundary[coords[:, 1], coords[:, 0]]):
                return Observation("occupied", ratio)
            if area >= self.min_area and min(w, h) / max(w, h) >= 0.1:
                candidates.append(area)
        return Observation("candidate" if candidates else "empty", ratio)


class IncrementalDetector(Detector):
    """空の基準と通知済み状態の両方を使い、追加された変化だけを評価する。"""

    def __init__(
        self,
        baseline: Frame,
        threshold: int = 30,
        min_area: int = 40,
        occupied_ratio: float = 0.18,
        color_threshold: int = 20,
    ) -> None:
        super().__init__(baseline, threshold, min_area, occupied_ratio, color_threshold)
        self.accept(baseline)

    def accept(self, frame: Frame) -> None:
        """通知済みの状態として frame を覚え、以降はこの状態からの追加分だけを評価する。"""
        self.valid_region(frame)
        self.reference_image = frame.copy()
        self.reference = Detector(
            frame, self.threshold, self.min_area, self.occupied_ratio, self.color_threshold
        )

    def strength(self, frame: Frame) -> NDArray[np.floating[Any]]:
        gray: NDArray[np.floating[Any]] = (
            cv2.absdiff(self.gray(frame), self.baseline).astype(np.float32) / self.threshold
        )
        if not self.color_threshold:
            return gray
        color = np.max(cv2.absdiff(self.color(frame), self.baseline_color), axis=2)
        combined: NDArray[np.floating[Any]] = np.maximum(
            gray, color.astype(np.float32) / self.color_threshold
        )
        return combined

    def analyze(self, frame: Frame, valid_mask: Mask | None = None) -> Observation:
        valid = self.valid_region(frame, valid_mask)
        dirty = self.difference_mask(frame, valid)
        if not np.any(dirty):
            return Observation("empty", 0.0)
        delta = self.reference.difference_mask(frame, valid)
        # 基準へ近づく変化（拭き取り・乾燥など）を追加物体として扱わない。
        additions = (
            (delta > 0)
            & (dirty > 0)
            & (self.strength(frame) >= self.strength(self.reference_image) - 0.05)
        )
        mask = cv2.morphologyEx(
            additions.astype(np.uint8) * 255, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)
        )
        result = self.classify_mask(mask, valid)
        # 除去分を含む総差分ではなく、追加分に遮蔽・広範囲変動の基準を適用する。
        if result.score > 0.8:
            return Observation("unstable", result.score)
        return result

    def changed_from(self, frame: Frame, previous: Frame, valid_mask: Mask | None = None) -> bool:
        detector = Detector(
            previous, self.threshold, self.min_area, self.occupied_ratio, self.color_threshold
        )
        valid = self.valid_region(frame, valid_mask)
        return bool(np.count_nonzero(detector.difference_mask(frame, valid)) >= self.min_area)
