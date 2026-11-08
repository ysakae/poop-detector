"""画像差分は排泄物の識別ではなく変化の候補を返す。"""

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Observation:
    kind: str
    score: float


class Detector:
    def __init__(
        self, baseline, threshold=30, min_area=40, occupied_ratio=0.18, color_threshold=20
    ):
        self.shape = baseline.shape
        self.baseline = self.gray(baseline)
        self.threshold = threshold
        self.min_area = min_area
        self.occupied_ratio = occupied_ratio
        self.color_threshold = color_threshold
        self.baseline_color = self.color(baseline)

    @staticmethod
    def color(frame):
        return cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2LAB), (5, 5), 0)[:, :, 1:]

    @staticmethod
    def gray(frame):
        return cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0)

    def analyze(self, frame, valid_mask=None):
        if frame.shape != self.shape:
            raise ValueError("基準画像と解像度が異なります。基準画像を再登録してください")
        if valid_mask is None:
            valid = np.ones(frame.shape[:2], dtype=bool)
        else:
            valid = np.asarray(valid_mask) > 0
            if valid.shape != frame.shape[:2] or not np.any(valid):
                raise ValueError("監視マスクの形状または面積が不正です")
        gray = self.gray(frame)
        signed = gray.astype(np.int16) - self.baseline.astype(np.int16)
        if float(np.mean(np.abs(signed[valid]) > self.threshold)) > 0.8:
            return Observation("unstable", 0.0)
        diff = cv2.absdiff(gray, self.baseline)
        mask = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)[1]
        if self.color_threshold:
            color_diff = cv2.absdiff(self.color(frame), self.baseline_color)
            mask[np.max(color_diff, axis=2) > self.color_threshold] = 255
        mask[~valid] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask[~valid] = 0
        ratio = float(np.count_nonzero(mask)) / np.count_nonzero(valid)
        if ratio >= self.occupied_ratio:
            return Observation("occupied", ratio)
        contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
        candidates = []
        height, width = mask.shape
        margin = max(1, round(min(height, width) * 0.01))
        # 斜辺やくぼみも含めた監視領域の境界からの距離を使う。
        padded = np.pad(valid.astype(np.uint8), 1)
        distance = cv2.distanceTransform(padded, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)[1:-1, 1:-1]
        boundary = valid & (distance <= margin + 1)
        for contour in contours:
            area = cv2.contourArea(contour)
            x, y, w, h = cv2.boundingRect(contour)
            # ROI外から入り込んだ体の一部は、面積が小さくても遮蔽として保留する。
            coords = contour[:, 0, :]
            if area >= self.min_area and np.any(boundary[coords[:, 1], coords[:, 0]]):
                return Observation("occupied", ratio)
            if area >= self.min_area and min(w, h) / max(w, h) >= 0.1:
                candidates.append(area)
        return Observation("candidate" if candidates else "empty", ratio)
