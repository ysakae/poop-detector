"""現在の映像と直近の判定が、ローカルGUIへ表示されることを確認する。"""

import json
import os
import time
from contextlib import closing
from http.client import HTTPConnection

import cv2
import numpy as np
from support import request, session_cookie

from poop_detector.capture.live_preview import LivePreview
from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.clips import ClipStore


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
        clip_store.assessments.save(
            clip_id, Assessment(label=label, activity="excreting", reason="<根拠>")
        )
    clip_store.labels.save(latest, ClipLabel("none", activity="feeding"))

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
    assert data["requested_at"]
    assert data["assessed_at"]


def test_live_preview_rejects_other_hosts(gui):
    server, _, _ = gui
    for path in ("/live/frame", "/live/assessment"):
        assert request(server, "GET", path, headers={"Host": "example.com"})[0] == 403


def test_monitoring_command_shares_frames_without_clip_recording(gui, monkeypatch):
    from poop_detector.commands import context
    from poop_detector.detection.roi import ROI
    from poop_detector.detection.scene import save_baseline
    from poop_detector.main import main

    server, store, _ = gui
    monkeypatch.chdir(store.root)
    frame = np.full((60, 100, 3), 190, dtype=np.uint8)
    roi = ROI(0, 0, 1, 1)
    roi.save(store.root / "roi.json")
    save_baseline(store.root / "baseline" / "empty.png", frame, roi)
    config = store.root / "test-config.yaml"
    config.write_text(
        f"data_dir: {store.root}\nclip_enabled: false\nnotifications: false\nfps: 20\n"
    )

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

    monkeypatch.setattr(context, "Camera", Camera)
    monkeypatch.setenv("POOP_RTSP_URL", "test-camera")
    assert request(server, "GET", "/live/frame")[0] == 503
    assert main(["--config", str(config), "run", "--max-frames", "2"]) == 0
    assert request(server, "GET", "/live/frame")[0] == 200


def test_preview_keeps_consecutive_camera_frames(tmp_path):
    preview = LivePreview(tmp_path)
    frame = np.zeros((60, 100, 3), dtype=np.uint8)
    assert preview.read() is None

    for tick in range(5):
        frame.fill(tick * 20)
        preview.publish(frame, now=tick / 20)
        snapshot = preview.read()
        assert snapshot is not None
        jpg, _ = snapshot
        decoded = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        assert decoded[0, 0, 0] == tick * 20


def test_live_stream_requires_session_and_same_site(gui):
    server, _, _ = gui
    for headers, auth in (
        ({}, False),
        ({"Host": "example.com"}, True),
        ({"Sec-Fetch-Site": "cross-site"}, True),
    ):
        assert request(server, "GET", "/live/stream", headers=headers, auth=auth)[0] == 403


def test_live_stream_sends_new_frames_on_one_request(gui):
    server, store, _ = gui
    preview = LivePreview(store.root)
    frame = np.zeros((60, 100, 3), dtype=np.uint8)

    with closing(HTTPConnection(*server.server_address, timeout=2)) as conn:
        conn.request("GET", "/live/stream", headers=session_cookie(server))
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "multipart/x-mixed-replace; boundary=frame"
        assert response.getheader("Cache-Control") == "no-store"
        for tick in range(2):
            frame.fill(tick * 120)
            preview.publish(frame, now=tick / 20)
            assert response.readline() == b"--frame\r\n"
            assert response.readline() == b"Content-Type: image/jpeg\r\n"
            length = int(response.readline().split(b":", 1)[1])
            assert response.readline() == b"\r\n"
            jpg = response.read(length)
            decoded = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            assert decoded[0, 0, 0] == tick * 120
            assert response.read(2) == b"\r\n"


def test_live_status_does_not_start_preview(gui):
    server, store, _ = gui
    preview = LivePreview(store.root)
    status, body = request(server, "GET", "/live/status")
    assert status == 200
    assert json.loads(body) == {"frame_time": None, "tray_state": "fixed"}
    assert not preview.request_path.exists()
    assert (
        request(server, "GET", "/live/status", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    )


def test_live_stream_rechecks_session(gui, monkeypatch):
    from poop_detector.gui import live_stream

    monkeypatch.setattr(live_stream, "SESSION_CHECK_INTERVAL", 0.15)
    server, store, _ = gui
    preview = LivePreview(store.root)
    cookie = session_cookie(server)
    frame = np.zeros((60, 100, 3), dtype=np.uint8)
    with closing(HTTPConnection(*server.server_address, timeout=2)) as conn:
        conn.request("GET", "/live/stream", headers=cookie)
        response = conn.getresponse()
        assert response.status == 200
        preview.publish(frame)
        assert response.readline() == b"--frame\r\n"
        server.app.sessions.revoke(cookie["Cookie"].split("=", 1)[1])
        assert response.read().endswith(b"\r\n--frame--\r\n")
