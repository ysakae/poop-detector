"""監視とGUIで最新フレームを1枚だけ共有する。"""

import logging
import os
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import cv2

from ..atomic_file import write_bytes
from ..frames import Frame

logger = logging.getLogger(__name__)
FRAME_INTERVAL = 0.03  # 書き出しの最短の間隔（秒）。約30fpsの入力も受け取る。
FRAME_MAX_AGE = 3  # これより古い画像は GUI に返さない（秒）
REQUEST_MAX_AGE = 5  # GUI からの要求がこれより古ければ書き出しをやめる（秒）
HEARTBEAT_MAX_AGE = 5  # 監視の稼働状態がこれより古ければ、監視が止まったとみなす（秒）


class LivePreview:
    """GUI から要求があったときだけ、最新フレームをメモリーかファイルで共有する。"""

    def __init__(self, root: str | Path, *, in_memory: bool = False) -> None:
        self.root = Path(root)
        self.frame_path = self.root / ".live-preview.jpg"
        self.request_path = self.root / ".live-preview-request"
        self.tray_state_path = self.root / ".live-preview-tray-state"
        self.last_attempt = float("-inf")
        self.last_file_error = float("-inf")
        self.monitor_path = self.root / ".live-preview-monitor"
        self.last_pulse = float("-inf")
        self.in_memory = in_memory
        self._requested_at = float("-inf")
        self._frame: tuple[bytes, float] | None = None

    def publish(self, frame: Frame | None, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if frame is None or now - self.last_attempt < FRAME_INTERVAL:
            return
        self.last_attempt = now
        local_request = now - self._requested_at <= REQUEST_MAX_AGE
        if not local_request:
            self._frame = None
        file_request = self._file_requested(now)
        if not local_request and not file_request:
            return
        try:
            height, width = frame.shape[:2]
            if max(width, height) > 640:
                scale = 640 / max(width, height)
                size = (max(1, round(width * scale)), max(1, round(height * scale)))
                frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
            ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok:
                return
            payload = jpeg.tobytes()
            if local_request:
                self._frame = (payload, time.time())
            if file_request:
                try:
                    write_bytes(self.frame_path, payload)
                except OSError:
                    self._file_error(now)
        except cv2.error:
            logger.warning("ライブ映像のプレビューを更新できません")
            self.last_attempt = now + REQUEST_MAX_AGE

    def _file_requested(self, now: float) -> bool:
        if now - self.last_file_error < REQUEST_MAX_AGE:
            return False
        try:
            return time.time() - self.request_path.stat().st_mtime <= REQUEST_MAX_AGE
        except FileNotFoundError:
            return False
        except OSError:
            self._file_error(now)
            return False

    def _file_error(self, now: float) -> None:
        logger.warning("別プロセス用のライブ映像を更新できません")
        self.last_file_error = now

    def set_tray_state(self, state: str) -> None:
        try:
            write_bytes(self.tray_state_path, state.encode("ascii"))
        except OSError:
            logger.warning("トレーの追従状態を保存できません")

    def tray_state(self) -> str:
        try:
            return self.tray_state_path.read_text(encoding="ascii")
        except (OSError, UnicodeError):
            return "fixed"

    def pulse(self, healthy: bool) -> None:
        """録画が無効・保留中でも監視とカメラの状態を共有する。"""
        now = time.monotonic()
        if now - self.last_pulse < 1:
            return
        self.last_pulse = now
        try:
            write_bytes(self.monitor_path, b"connected" if healthy else b"searching")
        except OSError:
            logger.warning("監視の稼働状態を保存できません")

    def mark_stopped(self) -> None:
        """監視が自分で終了したことを残す。GUI の見張り（features/watchdog.py）は知らせない。"""
        try:
            write_bytes(self.monitor_path, b"stopped")
        except OSError:
            logger.warning("監視の稼働状態を保存できません")

    def monitor_heartbeat(self) -> tuple[str, float] | None:
        """監視が最後に書いた (状態, UNIX 時刻)。監視を一度も動かしていなければ None。"""
        try:
            with self.monitor_path.open("r", encoding="ascii") as status:
                return status.read(), os.fstat(status.fileno()).st_mtime
        except (OSError, UnicodeError):
            return None

    def monitor_status(self) -> str:
        beat = self.monitor_heartbeat()
        if beat is None or beat[0] == "stopped" or time.time() - beat[1] > HEARTBEAT_MAX_AGE:
            return "offline"
        return beat[0]

    def read(self) -> tuple[bytes, str] | None:
        """GUI 側から呼び、最新の JPEG と撮影時刻を返す。呼ぶたびに監視側へ書き出しを要求する。"""
        self.request_frame()
        return self.latest()

    def request_frame(self) -> None:
        if self.in_memory:
            self._requested_at = time.monotonic()
            return
        with suppress(OSError):
            self.request_path.touch(mode=0o600)

    def _current_frame(self) -> tuple[bytes, float] | None:
        frame = self._frame
        if frame is not None and time.time() - frame[1] <= FRAME_MAX_AGE:
            return frame
        return None

    def latest(self, after: str | None = None) -> tuple[bytes, str] | None:
        """有効で、前回と異なる JPEG を返す。"""
        memory = self._current_frame()
        if memory is not None:
            jpeg, stamp = memory
            timestamp = datetime.fromtimestamp(stamp, UTC).isoformat()
            return (jpeg, timestamp) if timestamp != after else None
        try:
            with self.frame_path.open("rb") as frame:
                stamp = os.fstat(frame.fileno()).st_mtime
                if time.time() - stamp <= FRAME_MAX_AGE:
                    timestamp = datetime.fromtimestamp(stamp, UTC).isoformat()
                    if timestamp != after:
                        return frame.read(), timestamp
        except OSError:
            pass
        return None

    def frame_time(self) -> str | None:
        memory = self._current_frame()
        if memory is not None:
            return datetime.fromtimestamp(memory[1], UTC).isoformat()
        try:
            stamp = self.frame_path.stat().st_mtime
        except OSError:
            return None
        if time.time() - stamp <= FRAME_MAX_AGE:
            return datetime.fromtimestamp(stamp, UTC).isoformat()
        return None
