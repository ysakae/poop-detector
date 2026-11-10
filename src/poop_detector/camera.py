"""FFmpegバックエンドのタイムアウトと再接続を備える取得処理。"""

import logging
import time

import cv2


class Camera:
    """RTSP などの映像を OpenCV で読み、失敗したら待ってから接続し直す。"""

    def __init__(self, source, timeout_ms=5000, retry_seconds=2, factory=None):
        self.source = source
        self.timeout_ms = timeout_ms
        self.retry_seconds = retry_seconds
        self.factory = factory or cv2.VideoCapture
        self.capture = None
        self.next_retry = 0.0
        self.generation = 0

    def __enter__(self):
        return self

    def close(self):
        if self.capture is not None:
            self.capture.release()
            self.capture = None

    def __exit__(self, *args):
        self.close()

    def read(self):
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
                    return frame
        except (cv2.error, OSError):
            logging.getLogger(__name__).warning("映像取得でエラーが発生しました")
        self.close()
        self.next_retry = time.monotonic() + self.retry_seconds
        logging.getLogger(__name__).warning("映像を取得できません。待機後に再接続します")
        return None


class LatestCamera:
    """映像を継続して取り出し、最新の未処理フレームのみ保持する。"""

    def __init__(self, camera, on_frame=None):
        import threading

        self.camera = camera
        self.on_frame = on_frame
        self.generation = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._latest = None
        self._healthy = False
        self._last_frame_at = 0.0
        self._thread = threading.Thread(target=self._pump, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def _pump(self):
        with self.camera:
            while not self._stop.is_set():
                frame = self.camera.read()
                if self.on_frame is not None:
                    try:
                        self.on_frame(frame, self.camera.generation)
                    except Exception:
                        logging.getLogger(__name__).error(
                            "動画処理でエラーが発生しました。映像取得は継続します"
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
    def healthy(self):
        with self._lock:
            return (
                self._healthy
                and time.monotonic() - self._last_frame_at <= self.camera.timeout_ms / 1000
            )

    def wait_ready(self, timeout):
        return self._ready.wait(timeout)

    def read(self):
        with self._lock:
            latest = self._latest
            self._latest = None
        if latest is None:
            return None
        frame, self.generation = latest
        return frame

    def __exit__(self, *args):
        self._stop.set()
        self._thread.join(self.camera.timeout_ms / 1000 * 2 + 1)
