"""`poop-detector run` を偽のカメラで起動し、検知から記録・動画保存までを通して確認する。

カメラの取得スレッド、監視ループ、ライブ映像、動画の収集と FFmpeg での変換を実際に動かす。
差し替えるのは RTSP の接続（Camera）だけ。
"""

import shutil
import time

import pytest
from support import spot_scene

from poop_detector import main as cli
from poop_detector.clips import ClipStore
from poop_detector.config import CONNECTION_KEYS
from poop_detector.events import Store
from poop_detector.main import main
from poop_detector.roi import ROI
from poop_detector.scene import save_baseline

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg が必要です")


class ScriptedCamera:
    """最初の一定時間は空のトイレを映し、その後は変化のある画像を映し続ける。"""

    generation = 1
    timeout_ms = 100

    def __init__(self, empty, changed, empty_seconds):
        self.empty, self.changed = empty, changed
        self.change_at = time.monotonic() + empty_seconds

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        # 実際のカメラと同じく、フレームは一定の間隔で届く。
        time.sleep(0.01)
        frame = self.empty if time.monotonic() < self.change_at else self.changed
        return frame.copy()


def test_run_records_event_and_saves_clip(tmp_path, monkeypatch):
    empty, changed = spot_scene()
    root = tmp_path / "data"
    roi = ROI(0, 0, 1, 1)
    root.mkdir()
    roi.save(root / "roi.json")
    save_baseline(root / "baseline" / "empty.png", roi.crop(empty), roi)
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                f"data_dir: {root}",
                "fps: 30",
                "verify_seconds: 0.1",
                "clear_seconds: 0.1",
                "notifications: false",
                "clip_before_seconds: 1",
                "clip_after_seconds: 0",
                "clip_fps: 10",
                "clip_max_width: 160",
            ]
        )
    )
    monkeypatch.chdir(tmp_path)
    for key in CONNECTION_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://camera.invalid/live")
    monkeypatch.setattr(cli, "Camera", lambda *args: ScriptedCamera(empty, changed, 0.5))

    assert main(["--config", "config.yaml", "run", "--max-frames", "40"]) == 0

    events = Store(root).list()
    assert len(events) == 1
    assert events[0]["notified_at"] is None
    clips = ClipStore(root).list()
    assert [(clip["event_id"], clip["source"], clip["status"]) for clip in clips] == [
        (events[0]["id"], "detected", "ready")
    ]
    assert (root / clips[0]["path"]).stat().st_size > 0
