"""留守中のダイジェストの静止画を残し、タイムラプスの動画を作る。

常時録画はしないので、家族が「外出中」にしている間（records.away_periods）だけ、
位置合わせ前の映像を SNAPSHOT_WIDTH に縮めた JPEG を digest/<区間の ID>/ に一定の間隔で残す。
KEEP_DAYS より古い区間の静止画は消す。バックアップの ALLOWED には入れない。
"""

import logging
import re
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import cv2

from ..atomic_file import write_bytes
from ..detection.monitoring import MonitorExtension
from ..frames import Frame
from ..records.away_periods import AwayPeriods
from ..timezones import local_now
from ..video.recording import ENCODE_TIMEOUT, encode_mp4

logger = logging.getLogger(__name__)
DIGEST_DIR = "digest"
SNAPSHOT_WIDTH = 640
KEEP_DAYS = 7
# タイムラプスの再生の速さ（1秒に何枚）。1分ごとの静止画なら、1時間が5秒になる。
TIMELAPSE_FPS = 12
TIMELAPSE_NAME = "timelapse.mp4"
# 変換を打ち切る秒数は、静止画1枚につきこの秒数（最低は録画と同じ ENCODE_TIMEOUT）。
# 1日分（1440枚）で72秒。手元の Apple Silicon では1日分が1秒ほどで終わるが、
# 何日も続いた外出や、遅いマシン（Docker・Linux）でも打ち切らないよう大きめにとる。
TIMELAPSE_SECONDS_PER_FRAME = 0.05
# 古い区間を消すかを確かめる間隔（秒）。
CLEANUP_SECONDS = 3600
_ID = re.compile(r"[0-9a-f]{32}")


class DigestSnapshots:
    """data_dir/digest/ の下の、留守の区間ごとの静止画。"""

    def __init__(self, root: str | Path) -> None:
        self.base = Path(root) / DIGEST_DIR

    def folder(self, period_id: str) -> Path:
        if not _ID.fullmatch(period_id):
            raise KeyError(period_id)
        return self.base / period_id

    def save(self, period_id: str, frame: Frame, at: datetime) -> Path:
        """frame を縮めて保存する。名前は撮った時刻（UTC のミリ秒）なので、名前の順が時刻の順。"""
        height, width = frame.shape[:2]
        scale = min(1.0, SNAPSHOT_WIDTH / width)
        # MP4（yuv420p）にできるよう、幅と高さを偶数にそろえる。
        size = (max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2))
        small = cv2.resize(frame, size) if size != (width, height) else frame
        ok, encoded = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            raise OSError("留守中の静止画を作れません")
        folder = self.folder(period_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{round(at.timestamp() * 1000):015d}.jpg"
        write_bytes(path, encoded.tobytes())
        return path

    def frames(self, period_id: str) -> list[Path]:
        folder = self.folder(period_id)
        return sorted(folder.glob("*.jpg")) if folder.is_dir() else []

    def cleanup(self, now: float | None = None) -> int:
        """KEEP_DAYS より前から更新のない区間のフォルダーを消し、消した数を返す。"""
        if not self.base.is_dir():
            return 0
        limit = (now if now is not None else time.time()) - KEEP_DAYS * 86400
        removed = 0
        for folder in self.base.iterdir():
            if folder.is_dir() and _ID.fullmatch(folder.name) and folder.stat().st_mtime < limit:
                shutil.rmtree(folder)
                removed += 1
        return removed


def timelapse(snapshots: DigestSnapshots, period_id: str) -> Path | None:
    """区間の静止画をタイムラプスの MP4 にする。静止画が増えていなければ作り直さない。"""
    frames = snapshots.frames(period_id)
    if not frames:
        return None
    path = snapshots.folder(period_id) / TIMELAPSE_NAME
    newest = max(frame.stat().st_mtime_ns for frame in frames)
    if path.is_file() and path.stat().st_mtime_ns >= newest:
        return path
    samples = [
        (index / TIMELAPSE_FPS, "", frame.read_bytes()) for index, frame in enumerate(frames)
    ]
    temporary = path.with_suffix(".tmp.mp4")
    timeout = max(ENCODE_TIMEOUT, len(frames) * TIMELAPSE_SECONDS_PER_FRAME)
    encode_mp4(samples, temporary, TIMELAPSE_FPS, timeout=timeout)
    temporary.replace(path)
    return path


class SnapshotExtension(MonitorExtension):
    """外出中の間だけ、位置合わせ前の映像を interval 秒ごとに静止画として残す。"""

    def __init__(
        self,
        periods: AwayPeriods,
        snapshots: DigestSnapshots,
        interval: float,
        *,
        clock: Callable[[], datetime] = local_now,
    ) -> None:
        self.periods, self.snapshots, self.interval, self.clock = (
            periods,
            snapshots,
            interval,
            clock,
        )
        self.last_sample = float("-inf")
        self.last_cleanup = float("-inf")

    def on_camera_frame(self, raw: Frame | None, disconnected: bool, tick: float) -> None:
        del disconnected
        if raw is None or tick - self.last_sample < self.interval:
            return
        self.last_sample = tick
        try:
            if tick - self.last_cleanup >= CLEANUP_SECONDS:
                self.last_cleanup = tick
                self.snapshots.cleanup()
            period = self.periods.current()
            if period is not None:
                self.snapshots.save(period.id, raw, self.clock())
        except (OSError, cv2.error):
            logger.warning("留守中の静止画を保存できませんでした")
