"""現在の映像と直近の判定が、ローカルGUIへ表示されることを確認する。"""

import json
import os
import time

import cv2
import numpy as np
from test_label_gui import gui as gui
from test_label_gui import request

from poop_detector.clips import ClipStore
from poop_detector.live_preview import LivePreview


def test_live_image_requires_a_viewer_and_expires(gui):
    server, store, _ = gui
    preview = LivePreview(store.root)
    frame = np.zeros((600, 1000, 3), dtype=np.uint8)
    preview.publish(frame, now=0)
    assert not preview.frame_path.exists()

    assert request(server, "GET", "/live/frame")[0] == 503
    preview.publish(frame, now=1)
    status, jpg = request(server, "GET", "/live/frame")
    assert status == 200
    assert cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR).shape == (384, 640, 3)

    stale = time.time() - 20
    os.utime(preview.frame_path, (stale, stale))
    assert request(server, "GET", "/live/frame")[0] == 503
    os.utime(preview.request_path, (stale, stale))
    preview.publish(frame, now=2)
    assert preview.frame_path.stat().st_mtime == stale


def test_home_shows_latest_codex_assessment_and_manual_label(gui):
    server, store, _ = gui
    clip_store = ClipStore(store.root)
    old = clip_store.request()
    latest = clip_store.request()
    for clip_id, label in ((old, "urine"), (latest, "feces")):
        clip_store.update(clip_id, status="ready", duration=10)
        clip_store.save_assessment(clip_id, label=label, activity="excreting", reason="<根拠>")
    clip_store.label(latest, "none", activity="feeding")

    status, page = request(server, "GET", "/")
    assert status == 200
    assert "現在のカメラ映像" in page.decode()
    assert b'src="/assets/live.js"' in page
    status, response = request(server, "GET", "/live/assessment")
    assert status == 200
    data = json.loads(response)
    assert data["clip_id"] == latest
    assert data["label"] == "排便"
    assert data["human_label"] == "排泄なし"
    assert data["reason"] == "<根拠>"
    assert data["requested_at"] and data["assessed_at"]


def test_live_preview_rejects_other_hosts(gui):
    server, _, _ = gui
    for path in ("/live/frame", "/live/assessment"):
        assert request(server, "GET", path, headers={"Host": "example.com"})[0] == 403


def test_monitoring_command_shares_frames_without_clip_recording(gui, monkeypatch):
    from poop_detector import main as cli
    from poop_detector.roi import ROI

    server, store, _ = gui
    frame = np.full((60, 100, 3), 190, dtype=np.uint8)
    roi = ROI(0, 0, 1, 1)
    roi.save(store.root / "roi.json")
    cli.save_baseline(store.root / "baseline" / "empty.png", frame, roi)
    config = store.root / "test-config.yaml"
    config.write_text(f"data_dir: {store.root}\nclip_enabled: false\nfps: 20\n")

    class Camera:
        def __init__(self, source, timeout_ms, retry_seconds):
            self.timeout_ms = timeout_ms
            self.generation = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            time.sleep(0.02)
            return frame.copy()

    monkeypatch.setattr(cli, "Camera", Camera)
    monkeypatch.setenv("POOP_RTSP_URL", "test-camera")
    assert request(server, "GET", "/live/frame")[0] == 503
    assert cli.main(["--config", str(config), "run", "--max-frames", "2"]) == 0
    assert request(server, "GET", "/live/frame")[0] == 200
