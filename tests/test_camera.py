"""カメラの再接続と、最新フレームだけを保持する取得スレッドを検証する。"""

import threading

import numpy as np
from support import spot_scene

from poop_detector.camera import Camera, LatestCamera


def test_camera_reconnects_and_releases():
    baseline, _ = spot_scene()

    class Capture:
        def __init__(self, succeeds):
            self.succeeds = succeeds
            self.released = False

        def isOpened(self):
            return True

        def read(self):
            return self.succeeds, baseline if self.succeeds else None

        def release(self):
            self.released = True

    captures = [Capture(False), Capture(True)]
    opened = []

    def factory(*args):
        capture = captures[len(opened)]
        opened.append(capture)
        return capture

    camera = Camera("rtsp://user:secret@localhost/live", factory=factory, retry_seconds=0)
    with camera:
        assert camera.read() is None
        assert camera.read().shape == baseline.shape
    assert all(c.released for c in captures)


def test_latest_camera_discards_old_frames():
    done = threading.Event()
    finish = threading.Event()
    baseline, _ = spot_scene()

    class Source:
        generation = 1
        timeout_ms = 100

        def __enter__(self):
            return self

        def __exit__(self, *args):
            finish.set()

        def read(self):
            if done.is_set():
                finish.wait(1)
                return None
            return baseline

    source = Source()
    with LatestCamera(source) as camera:
        assert camera.wait_ready(1)
        assert camera.read() is not None
        done.set()
    assert finish.is_set()


def test_latest_camera_overwrites_pending_frame():
    ready = threading.Event()
    release = threading.Event()

    class Source:
        generation = 1
        timeout_ms = 100
        count = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            self.count += 1
            if self.count > 3:
                ready.set()
                release.wait(1)
                return None
            return np.full((2, 2, 3), self.count, np.uint8)

    with LatestCamera(Source()) as camera:
        try:
            assert ready.wait(1)
            assert int(camera.read()[0, 0, 0]) == 3
            assert camera.read() is None
        finally:
            release.set()
