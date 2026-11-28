"""現在の映像と直近の判定が、ローカルGUIへ表示されることを確認する。"""

import json
import os
import threading
import time
from contextlib import closing
from http.client import HTTPConnection

import cv2
import numpy as np
import pytest
from support import request, session_cookie

from poop_detector.capture.live_preview import LivePreview
from poop_detector.gui.assets import asset_url
from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.clips import ClipStore


def test_combined_gui_keeps_jpeg_in_memory_and_serves_separate_gui(tmp_path, monkeypatch):
    from poop_detector.capture import live_preview

    monkeypatch.setattr(live_preview.time, "monotonic", lambda: 1)
    monitor = LivePreview(tmp_path, in_memory=True)
    standalone = LivePreview(tmp_path)
    frame = np.full((480, 640, 3), 100, np.uint8)

    monitor.request_frame()
    monitor.publish(frame, now=1)
    jpeg, stamp = monitor.latest()
    assert jpeg.startswith(b"\xff\xd8")
    assert monitor.frame_time() == stamp
    assert monitor.latest(after=stamp) is None
    assert not monitor.request_path.exists()
    assert not monitor.frame_path.exists()

    standalone.request_frame()
    monitor.publish(frame, now=2)
    assert standalone.latest()[0] == monitor.latest()[0]
    assert monitor.frame_path.exists()

    # 別プロセス向けの書き出しが失敗しても、同じプロセスの配信は続く。
    def full(*args):
        raise OSError("容量不足")

    monkeypatch.setattr(live_preview, "write_bytes", full)
    monitor.publish(frame, now=3)
    assert monitor.latest(after=stamp) is not None
    stale = time.time() + 4
    monkeypatch.setattr(live_preview.time, "time", lambda: stale)
    assert monitor.latest() is None
    assert monitor.frame_time() is None


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
    assert f'src="{asset_url("live.js")}"'.encode() in page
    status, response = request(server, "GET", "/live/assessment")
    assert status == 200
    data = json.loads(response)
    assert data["clip_id"] == latest
    assert data["label"] == "うんち"
    assert data["human_label"] == "うんち・おしっこなし"
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


def test_live_panel_recovers_when_stream_never_delivers_an_image(gui):
    playwright = pytest.importorskip("playwright.sync_api")
    server, store, _ = gui
    preview = LivePreview(store.root)
    preview.request_frame()
    frame = np.full((60, 100, 3), 190, dtype=np.uint8)
    frame[:, :50] = (150, 115, 60)
    host, port = server.server_address
    url = f"http://{host}:{port}"
    name, value = session_cookie(server)["Cookie"].split("=", 1)

    def publish_before_status(route):
        preview.request_frame()
        preview.publish(frame)
        route.continue_()

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page()
        page.context.add_cookies([{"name": name, "value": value, "url": url}])
        # 配信の要求を保留して、サーバーに新しい JPEG があっても画像が届かない状況を作る。
        page.route("**/live/stream*", lambda route: None)
        page.route("**/live/status", publish_before_status)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("document.querySelector('#live-frame').hasAttribute('src')")
        assert page.locator("#live-frame").is_hidden()
        page.wait_for_function(
            """() => {
                const image = document.querySelector('#live-frame');
                return !image.hidden && image.naturalWidth === 100;
            }""",
            timeout=8000,
        )
        playwright.expect(page.locator("#live-saver")).to_have_attribute("aria-pressed", "false")
        with page.expect_request("**/live/frame*"):
            pass
        with page.expect_request("**/live/stream?fps=2&t=*"):
            page.locator("#live-saver").click()
        browser.close()


def test_live_panel_retries_when_status_request_stalls(gui):
    playwright = pytest.importorskip("playwright.sync_api")
    server, store, _ = gui
    preview = LivePreview(store.root)
    frame = np.full((60, 100, 3), 190, dtype=np.uint8)
    host, port = server.server_address
    url = f"http://{host}:{port}"
    name, value = session_cookie(server)["Cookie"].split("=", 1)

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page()
        page.context.add_cookies([{"name": name, "value": value, "url": url}])

        def publish_before_status(route):
            preview.request_frame()
            preview.publish(frame)
            route.continue_()

        page.route("**/live/status", publish_before_status)
        # 復帰時に通信が応答しなくなった状況を、最初の要求を保留して再現する。
        page.route("**/live/status", lambda route: None, times=1)
        page.route("**/live/stream*", lambda route: None)
        page.goto(url, wait_until="domcontentloaded")
        playwright.expect(page.locator("#live-frame")).to_be_visible(timeout=12000)
        assert page.locator("#live-frame").evaluate("(image) => image.naturalWidth") == 100
        browser.close()


def test_live_panel_reconnects_after_stream_ends(gui):
    playwright = pytest.importorskip("playwright.sync_api")
    server, store, _ = gui
    preview = LivePreview(store.root)
    frame = np.full((60, 100, 3), 190, dtype=np.uint8)
    host, port = server.server_address
    url = f"http://{host}:{port}"
    name, value = session_cookie(server)["Cookie"].split("=", 1)
    camera_up = threading.Event()
    camera_up.set()
    stopped = threading.Event()

    def camera():
        # 監視の代わりに、要求があるときだけ最新のコマを書き出す。
        while not stopped.is_set():
            if camera_up.is_set():
                preview.publish(frame)
            time.sleep(0.05)

    thread = threading.Thread(target=camera, daemon=True)
    thread.start()
    shown = """() => {
        const image = document.querySelector('#live-frame');
        return !image.hidden && image.naturalWidth === 100;
    }"""
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            page = browser.new_page()
            page.context.add_cookies([{"name": name, "value": value, "url": url}])
            streams = []
            page.on("request", lambda req: "/live/stream" in req.url and streams.append(req.url))
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function(shown, timeout=8000)
            # カメラが数秒止まると、サーバーは配信を閉じる。
            camera_up.clear()
            page.wait_for_function("document.querySelector('#live-frame').hidden", timeout=10000)
            camera_up.set()
            # 同じ URL を開き直すと、ブラウザーは閉じた配信の画像を使い回して要求を送らない。
            page.wait_for_function(shown, timeout=10000)
            assert len(set(streams)) >= 2
            browser.close()
    finally:
        stopped.set()
        thread.join()
