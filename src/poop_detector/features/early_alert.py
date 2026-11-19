"""トイレ領域内で動いた後、同じ場所にとどまる場面を検知する。"""

import cv2
import numpy as np
from numpy.typing import NDArray

from ..config import Config
from ..detection.difference import Detector
from ..frames import Frame, Mask

# 滞在の長さと通知の間隔は設定（early_alert.stay_seconds・cooldown_seconds）で変えられる。
# 以下は、長辺240画素に縮小した画像で調整した判定の内部の値。カメラや画角ではなく
# 縮小後の画像に対する比率なので、利用者が変えると誤検知や見逃しが増えるため設定にしない。
SAMPLE_SECONDS = 1  # 標本を取る間隔（秒）。2倍を超えて空いたら滞在を数え直す
MIN_FOREGROUND_RATIO = 0.09  # 犬とみなす前景の最小の面積（監視範囲に対する割合）
MAX_FOREGROUND_RATIO = 0.8  # これを超える変化は照明などの全体の変化として扱う
MIN_OVERLAP = 0.64  # 前の標本と同じ場所とみなす重なり（IoU）
MOTION_PIXEL_DELTA = 16  # 動いたとみなす明るさの差
MIN_MOTION_RATIO = 0.08  # 滞在の始まりとみなす、前景のうち動いた部分の割合


class EarlyAlert:
    """録画の完了を待たず、トイレの同じ場所での滞在を見つけて知らせる。"""

    def __init__(self, baseline: Frame, config: Config) -> None:
        height, width = baseline.shape[:2]
        scale = 240 / max(width, height)
        self.size = (max(1, round(width * scale)), max(1, round(height * scale)))
        self.detector = Detector(
            cv2.resize(baseline, self.size),
            threshold=config.threshold,
            color_threshold=config.color_threshold,
        )
        self.stay_seconds = config.early_alert_stay_seconds
        self.cooldown_seconds = config.early_alert_cooldown_seconds
        self.last_alert = float("-inf")
        self.reset()

    def reset(self) -> None:
        self.sampled_at = float("-inf")
        self.anchor: NDArray[np.bool_] | None = None
        self.started_at: float | None = None
        self.alerted = False
        self.previous_gray: Frame | None = None

    def update(self, frame: Frame, valid_mask: Mask | None, now: float) -> bool:
        """1秒ごとに標本を取り、同じ場所に stay_seconds とどまったら1回だけ True を返す。"""
        elapsed = now - self.sampled_at
        if elapsed < SAMPLE_SECONDS:
            return False
        if elapsed > 2 * SAMPLE_SECONDS:
            self.reset()
        self.sampled_at = now
        measurement = self._measure(frame, valid_mask)
        if measurement is None:
            self.anchor = None
            return False
        foreground, moving = measurement
        return self._advance(foreground, moving, now)

    def _measure(
        self, frame: Frame, valid_mask: Mask | None
    ) -> tuple[NDArray[np.bool_], bool] | None:
        """有効領域の最大の前景と、その前景内に動きがあったかを返す。"""
        image = cv2.resize(frame, self.size)
        if valid_mask is not None:
            valid_mask = cv2.resize(valid_mask, self.size, interpolation=cv2.INTER_NEAREST)
            if not np.any(valid_mask):
                self.previous_gray = None
                return None
        valid = self.detector.valid_region(image, valid_mask)
        previous = self.previous_gray
        gray = self.previous_gray = self.detector.gray(image)
        valid_area = np.count_nonzero(valid)
        mask = self.detector.difference_mask(image, valid)
        if cv2.countNonZero(mask) > MAX_FOREGROUND_RATIO * valid_area:
            return None
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        if count == 1:
            return None
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        if stats[largest, cv2.CC_STAT_AREA] < MIN_FOREGROUND_RATIO * valid_area:
            return None

        foreground = labels == largest
        foreground_area = stats[largest, cv2.CC_STAT_AREA]
        moving = False
        if previous is not None:
            changed = cv2.absdiff(gray, previous) >= MOTION_PIXEL_DELTA
            moving = bool(
                np.count_nonzero(changed & foreground) >= MIN_MOTION_RATIO * foreground_area
            )
        return foreground, moving

    def _advance(self, foreground: NDArray[np.bool_], moving: bool, now: float) -> bool:
        if self.anchor is None or np.count_nonzero(self.anchor & foreground) < (
            MIN_OVERLAP * np.count_nonzero(self.anchor | foreground)
        ):
            self.anchor = foreground
            self.started_at = None
            self.alerted = False
        if self.started_at is None:
            # 起動時から静止している犬や物体は、基準画像との差だけで滞在とみなさない。
            if moving:
                self.anchor = foreground
                self.started_at = now
            return False
        if self.alerted or now - self.started_at < self.stay_seconds:
            return False
        self.alerted = True
        if now - self.last_alert < self.cooldown_seconds:
            return False
        self.last_alert = now
        return True
