"""トイレ領域内で動いた後、同じ場所にとどまる場面を検知する。"""

import cv2
import numpy as np

from .detection import Detector

SAMPLE_SECONDS = 1
STAY_SECONDS = 4
COOLDOWN_SECONDS = 30
MIN_FOREGROUND_RATIO = 0.09
MAX_FOREGROUND_RATIO = 0.8
MIN_OVERLAP = 0.64
MOTION_PIXEL_DELTA = 16
MIN_MOTION_RATIO = 0.08


class EarlyAlert:
    def __init__(self, baseline, config):
        height, width = baseline.shape[:2]
        scale = 240 / max(width, height)
        self.size = (max(1, round(width * scale)), max(1, round(height * scale)))
        self.detector = Detector(
            cv2.resize(baseline, self.size),
            threshold=config.threshold,
            color_threshold=config.color_threshold,
        )
        self.last_alert = float("-inf")
        self.reset()

    def reset(self):
        self.sampled_at = float("-inf")
        self.anchor = None
        self.started_at = None
        self.alerted = False
        self.previous_gray = None

    def update(self, frame, valid_mask, now):
        elapsed = now - self.sampled_at
        if elapsed < SAMPLE_SECONDS:
            return False
        if elapsed > 2 * SAMPLE_SECONDS:
            self.reset()
        self.sampled_at = now
        image = cv2.resize(frame, self.size)
        if valid_mask is not None:
            valid_mask = cv2.resize(valid_mask, self.size, interpolation=cv2.INTER_NEAREST)
            if not np.any(valid_mask):
                self.anchor = None
                self.previous_gray = None
                return False
        valid = self.detector.valid_region(image, valid_mask)
        previous = self.previous_gray
        self.previous_gray = self.detector.gray(image)
        valid_area = np.count_nonzero(valid)
        mask = self.detector.difference_mask(image, valid)
        if cv2.countNonZero(mask) > MAX_FOREGROUND_RATIO * valid_area:
            self.anchor = None
            return False
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        if count == 1:
            self.anchor = None
            return False
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        if stats[largest, cv2.CC_STAT_AREA] < MIN_FOREGROUND_RATIO * valid_area:
            self.anchor = None
            return False

        foreground = labels == largest
        foreground_area = stats[largest, cv2.CC_STAT_AREA]
        if self.anchor is None or np.count_nonzero(self.anchor & foreground) < (
            MIN_OVERLAP * np.count_nonzero(self.anchor | foreground)
        ):
            self.anchor = foreground
            self.started_at = None
            self.alerted = False
        if self.started_at is None:
            # 起動時から静止している犬や物体は、基準画像との差だけで滞在とみなさない。
            if previous is not None:
                changed = cv2.absdiff(self.previous_gray, previous) >= MOTION_PIXEL_DELTA
                if np.count_nonzero(changed & foreground) >= MIN_MOTION_RATIO * foreground_area:
                    self.anchor = foreground
                    self.started_at = now
            return False
        if self.alerted or now - self.started_at < STAY_SECONDS:
            return False
        self.alerted = True
        if now - self.last_alert < COOLDOWN_SECONDS:
            return False
        self.last_alert = now
        return True
