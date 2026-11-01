"""画像差分は排泄物の識別ではなく変化の候補を返す。"""

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Observation:
    kind: str
    score: float


class Detector:
    def __init__(self, baseline, threshold=30, min_area=40, occupied_ratio=0.18):
        self.shape = baseline.shape
        self.baseline = self.gray(baseline)
        self.threshold = threshold
        self.min_area = min_area
        self.occupied_ratio = occupied_ratio

    @staticmethod
    def gray(frame):
        return cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0)

    def analyze(self, frame):
        if frame.shape != self.shape:
            raise ValueError("基準画像と解像度が異なります。基準画像を再登録してください")
        gray = self.gray(frame)
        signed = gray.astype(np.int16) - self.baseline.astype(np.int16)
        if float(np.mean(np.abs(signed) > self.threshold)) > 0.8:
            return Observation("unstable", 0.0)
        diff = cv2.absdiff(gray, self.baseline)
        mask = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)[1]
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        ratio = float(np.count_nonzero(mask)) / mask.size
        if ratio >= self.occupied_ratio:
            return Observation("occupied", ratio)
        contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
        candidates = []
        for contour in contours:
            area = cv2.contourArea(contour)
            _, _, w, h = cv2.boundingRect(contour)
            if area >= self.min_area and min(w, h) / max(w, h) >= 0.1:
                candidates.append(area)
        return Observation("candidate" if candidates else "empty", ratio)
