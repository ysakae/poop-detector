"""犬がサークルにいない区間を記録し、外出中に続いたら脱走の可能性として知らせる。

在室の観測は睡眠の記録と同じ MotionSampler（still・moving・left・unknown）を使う。
丸まって寝ている犬は検出が当たらないので、検出が外れただけでは不在にしない。
「出ていった」（left）の証拠が出たときだけ不在を始め、still か moving で終える。
unknown は続いている不在を保つが、新しい不在は始めない。
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

import cv2

from ..config import Config
from ..detection.monitoring import MonitorExtension
from ..frames import Frame
from ..records.absences import Absences, AwayMode
from ..timezones import local_now
from .dog import Box, Predict
from .sleep import Presence

logger = logging.getLogger(__name__)
# 外出中の通知に失敗したとき、次に試すまでの秒数。
RETRY_SECONDS = 60


class PresenceNotifier(Protocol):
    """外出中の不在と、そのあとの戻りを知らせる通知先。"""

    def send_absence(self, since: datetime, minutes: float) -> bool: ...
    def send_return(self, minutes: float) -> bool: ...


class PresenceSampler(Protocol):
    """1秒ごとの在室の観測を作る（features.sleep_motion.MotionSampler）。"""

    def sample(self, frame: Frame, boxes: list[Box]) -> tuple[Presence, bool]: ...
    def reset(self) -> None: ...


class PresenceTracker:
    """在室の観測から不在の区間を決めて記録し、外出中なら知らせる。"""

    def __init__(
        self, absences: Absences, away: AwayMode, config: Config, notifier: PresenceNotifier
    ) -> None:
        self.absences, self.away, self.notifier = absences, away, notifier
        self.min_absence = timedelta(minutes=config.presence_min_minutes)
        self.away_after = timedelta(minutes=config.presence_away_minutes)
        self._reset()

    def _reset(self) -> None:
        # 不在が始まった時刻（最初の left）と、最後に left を確かめた時刻。
        self.since: datetime | None = None
        self.last_left: datetime | None = None
        self.row: str | None = None
        self.notified = False
        self.next_try: datetime | None = None

    def update(self, presence: Presence, now: datetime) -> None:
        """1回分の観測を受け取る。"""
        if presence in ("still", "moving"):
            self._finish(now)
            return
        if presence == "left":
            if self.since is None:
                self.since = now
            self.last_left = now
        if self.since is None:
            return
        absent = now - self.since
        if self.row is None and absent >= self.min_absence:
            self.row = self.absences.start(self.since, away=self.away.since() is not None)
        if self.row is not None and not self.notified and absent >= self.away_after:
            self._notify_absence(now, self.since, self.row)

    def interrupt(self, now: datetime) -> None:
        """映像が途切れたら、続いていた不在を最後に確かめた時刻で閉じる。"""
        del now
        if self.row is not None and self.last_left is not None:
            self.absences.end(self.row, self.last_left, interrupted=True)
        self._reset()

    def _notify_absence(self, now: datetime, since: datetime, row: str) -> None:
        if self.away.since() is None or (self.next_try is not None and now < self.next_try):
            return
        if self.notifier.send_absence(since, (now - since).total_seconds() / 60):
            self.notified = True
            self.absences.mark_notified(row)
            logger.info("外出中に犬がサークルにいないことを知らせました")
        else:
            self.next_try = now + timedelta(seconds=RETRY_SECONDS)

    def _finish(self, now: datetime) -> None:
        if self.row is not None and self.since is not None:
            self.absences.end(self.row, now)
            if self.notified:
                self.notifier.send_return((now - self.since).total_seconds() / 60)
        self._reset()


class PresenceRecorder(Protocol):
    """在室の観測を受け取る記録器（PresenceTracker）。"""

    def update(self, presence: Presence, now: datetime) -> None: ...
    def interrupt(self, now: datetime) -> None: ...


class PresenceExtension(MonitorExtension):
    """位置合わせ前の映像で1秒ごとに在室を観測し、不在を記録する（features.presence）。"""

    def __init__(
        self,
        recorder: PresenceRecorder,
        sampler: PresenceSampler,
        predict: Predict,
        *,
        clock: Callable[[], datetime] = local_now,
    ) -> None:
        self.recorder, self.sampler, self.predict, self.clock = recorder, sampler, predict, clock
        self.last_sample = float("-inf")

    def on_camera_frame(self, raw: Frame | None, disconnected: bool, tick: float) -> None:
        # トレー追従に失敗しても床面の映像は使えるので、接続の途切れだけで区切る。
        if disconnected:
            self.recorder.interrupt(self.clock())
            self.sampler.reset()
        if raw is None or tick - self.last_sample < 1:
            return
        self.last_sample = tick
        try:
            boxes = self.predict(raw)
        except (cv2.error, OSError, ValueError):
            logger.warning("在室の確認のための犬検出に失敗しました")
            self.recorder.update("unknown", self.clock())
            return
        presence, _ = self.sampler.sample(raw, boxes)
        self.recorder.update(presence, self.clock())
