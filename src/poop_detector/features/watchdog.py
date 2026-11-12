"""監視の停止とカメラの不調を管理者に知らせる見張り。

見張りは2つに分ける。

- カメラの不調: 監視自身が、映像の届かない時間を CameraWatchdogExtension で数える。
- 監視の停止: 止まった監視は自分で知らせられないので、GUI のサーバーが MonitorWatchdog で
  監視のハートビート（LivePreview.pulse のファイル）を見張る。監視が自分で終了したとき
  （LivePreview.mark_stopped）と、一度も動かしていないときは知らせない。
"""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from ..capture.live_preview import HEARTBEAT_MAX_AGE, LivePreview
from ..detection.monitoring import MonitorExtension
from ..frames import Frame
from .outages import OutageTracker, utc_stamp

logger = logging.getLogger(__name__)
# 監視ループの呼び出しがこれより空いたら、トレー交換などで保留していたとみなす（秒）。
PAUSE_SECONDS = 10.0
# GUI の見張りがハートビートを確かめる間隔（秒）。
CHECK_SECONDS = 10.0


class CameraWatchdogExtension(MonitorExtension):
    """カメラから映像が届かない時間を数え、続いたら知らせる（watchdog.enabled）。

    再接続や接続の途切れも、映像が届かない時間に含める。tick（単調時計）で長さを測り、
    wall（UNIX 時刻）で停止の始まりを記録する。
    """

    def __init__(self, tracker: OutageTracker, wall: Callable[[], float] = time.time) -> None:
        self.tracker, self.wall = tracker, wall
        self.last_frame: float | None = None
        self.last_call: float | None = None
        # 前回の監視が残した停止は、知らせずに閉じる（監視を止めていた時間を不調と数えない）。
        tracker.log.close_open(tracker.kind, utc_stamp(wall()))
        tracker.current = None

    def on_camera_frame(self, raw: Frame | None, disconnected: bool, tick: float) -> None:
        del disconnected
        if self.last_call is None or tick - self.last_call > PAUSE_SECONDS:
            self.last_frame = tick
        self.last_call = tick
        now = self.wall()
        if raw is not None or self.last_frame is None:
            self.last_frame = tick
            self.tracker.observe(None, now)
            return
        self.tracker.observe(now - (tick - self.last_frame), now)


class MonitorWatchdog:
    """GUI のサーバーから、監視のハートビートが途絶えていないかを確かめる。"""

    def __init__(
        self, preview: LivePreview, tracker: OutageTracker, wall: Callable[[], float] = time.time
    ) -> None:
        self.preview, self.tracker, self.wall = preview, tracker, wall

    def check(self) -> None:
        beat = self.preview.monitor_heartbeat()
        # 一度も動かしていない監視と、自分で終了した監視は知らせない。
        if beat is None or beat[0] == "stopped":
            return
        _state, stamp = beat
        now = self.wall()
        self.tracker.observe(None if now - stamp <= HEARTBEAT_MAX_AGE else stamp, now)


@contextmanager
def watching(check: Callable[[], None], interval: float = CHECK_SECONDS) -> Iterator[None]:
    """別スレッドで check を interval ごとに呼び、ブロックを抜けたら止める。"""
    stop = threading.Event()

    def loop() -> None:
        # 起動してすぐに1回確かめ、以後は interval ごとに確かめる。
        while True:
            try:
                check()
            except (OSError, ValueError, sqlite3.Error) as error:
                # 例外の文字列は出さず（パスなどを含みうる）、種類だけを残して次の間隔でやり直す。
                logger.warning("監視の見張りで確認できませんでした（%s）", type(error).__name__)
            if stop.wait(interval):
                return

    thread = threading.Thread(target=loop, name="poop-detector-watchdog", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
