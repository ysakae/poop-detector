"""FFmpegバックエンドのタイムアウトと再接続を備える取得処理。"""

import logging
import threading
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self

import cv2

from ..frames import Frame

# LatestCamera が取得したフレームごとに呼ぶ処理。引数はフレーム（取得失敗時は None）と接続の世代。
FrameCallback = Callable[[Frame | None, int], None]


class Camera:
    """RTSP などの映像を OpenCV で読み、失敗したら待ってから接続し直す。"""

    def __init__(
        self,
        source: str,
        timeout_ms: int = 5000,
        retry_seconds: float = 2,
        factory: Callable[..., Any] | None = None,
    ) -> None:
        self.source = source
        self.timeout_ms = timeout_ms
        self.retry_seconds = retry_seconds
        self.factory = factory or cv2.VideoCapture
        self.capture: Any = None
        self.next_retry = 0.0
        self.generation = 0

    def __enter__(self) -> Self:
        return self

    def close(self) -> None:
        if self.capture is not None:
            self.capture.release()
            self.capture = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def read(self) -> Frame | None:
        """1フレームを読む。失敗したら接続を閉じ、retry_seconds の間は None を返す。"""
        if time.monotonic() < self.next_retry:
            return None
        try:
            if self.capture is None:
                self.capture = self.factory(
                    self.source,
                    cv2.CAP_FFMPEG,
                    [
                        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                        self.timeout_ms,
                        cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                        self.timeout_ms,
                    ],
                )
                self.generation += 1
            if self.capture.isOpened():
                ok, frame = self.capture.read()
                if ok and frame is not None:
                    result: Frame = frame
                    return result
        except (cv2.error, OSError):
            logging.getLogger(__name__).warning("映像取得でエラーが発生しました")
        self.close()
        self.next_retry = time.monotonic() + self.retry_seconds
        logging.getLogger(__name__).warning("映像を取得できません。待機後に再接続します")
        return None


class LatestCamera:
    """映像を継続して取り出し、最新の未処理フレームのみ保持する。"""

    def __init__(self, camera: Camera, on_frame: FrameCallback | None = None) -> None:
        self.camera = camera
        self.on_frame = on_frame
        self.generation = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._latest: tuple[Frame | None, int] | None = None
        self._healthy = False
        self._last_frame_at = 0.0
        self._thread = threading.Thread(target=self._pump, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def _pump(self) -> None:
        with self.camera:
            while not self._stop.is_set():
                frame = self.camera.read()
                if self.on_frame is not None:
                    try:
                        self.on_frame(frame, self.camera.generation)
                    # 動画の保存やライブ映像など、差し込まれた処理の失敗で映像の取得を
                    # 止めないため、種類を問わず捕捉する。詳細は --verbose で伏せ字にして出す。
                    except Exception:
                        logging.getLogger(__name__).error(
                            "動画処理でエラーが発生しました。映像取得は継続します",
                            exc_info=logging.getLogger().isEnabledFor(logging.DEBUG),
                        )
                with self._lock:
                    self._latest = (frame, self.camera.generation)
                    self._healthy = frame is not None
                    if frame is not None:
                        self._last_frame_at = time.monotonic()
                if frame is not None:
                    self._ready.set()
                else:
                    self._stop.wait(0.05)

    @property
    def healthy(self) -> bool:
        """直近の取得が成功し、timeout_ms 以内に新しいフレームが届いているか。"""
        with self._lock:
            return (
                self._healthy
                and time.monotonic() - self._last_frame_at <= self.camera.timeout_ms / 1000
            )

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def read(self) -> Frame | None:
        """まだ処理していない最新のフレーム。前回の read 以降に届いていなければ None。"""
        with self._lock:
            latest = self._latest
            self._latest = None
        if latest is None:
            return None
        frame, self.generation = latest
        return frame

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join(self.camera.timeout_ms / 1000 * 2 + 1)
