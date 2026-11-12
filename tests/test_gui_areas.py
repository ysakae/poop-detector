"""管理 › 範囲の登録（監視範囲と基準画像、トレー外の範囲、床面。#88）を検証する。"""

import json
import re
import threading
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import urlencode

import cv2
import numpy as np
import pytest
from support import exchange, login

from poop_detector.capture.live_preview import LivePreview
from poop_detector.detection.roi import ROI
from poop_detector.features.activity_map import LAYOUT_FILE, ActivityLayout

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
PAGE = "/manage/areas"
FRAME_URL = "/manage/areas/frame"
SECRET_URL = "rtsp://user:secret-password@192.0.2.10/stream"
SQUARE = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]


def scene_frame():
    """縁のあるトレーを模した人工画像（test_tray_tracking.py と同じ。トレー追従の基準になる）。"""
    frame = np.full((360, 640, 3), 65, np.uint8)
    cv2.rectangle(frame, (170, 75), (489, 284), (222, 222, 222), -1)
    cv2.rectangle(frame, (186, 91), (473, 268), (140, 150, 165), -1)
    for x in range(188, 471, 30):
        for y in (81, 276):
            cv2.rectangle(frame, (x, y), (x + 8, y + 4), (115, 115, 115), -1)
    return frame


# scene_frame のトレーの外周（0〜1 の座標）。
TRAY = [
    [170 / 640, 75 / 360],
    [490 / 640, 75 / 360],
    [490 / 640, 285 / 360],
    [170 / 640, 285 / 360],
]


@pytest.fixture
def areas_gui(tmp_path, monkeypatch):
    from poop_detector.gui.server import make_server

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"POOP_RTSP_URL={SECRET_URL}\n")
    root = tmp_path / "data"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"data_dir: {root}\n")
    opened = []

    def camera(source, *_args):
        opened.append(source)
        return nullcontext(SimpleNamespace(read=scene_frame))

    monkeypatch.setattr("poop_detector.commands.context.Camera", camera)
    server = make_server(root, 0, config_path=config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(server=server, root=root, opened=opened)
    server.shutdown()
    server.server_close()
    thread.join()


def page(server, cookie, path=PAGE):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def snapshot(server, cookie):
    status, headers, body = exchange(server, "GET", FRAME_URL, headers=cookie, auth=False)
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert body[:2] == b"\xff\xd8"
    return headers["X-Snapshot-Id"]


def save(server, cookie, **fields):
    token = re.search(r'name="csrf" value="([^"]+)"', page(server, cookie))[1]
    values = {"points": json.dumps(SQUARE), **fields, "csrf": token}
    status, headers, body = exchange(
        server, "POST", PAGE, urlencode(values), {**FORM, **cookie}, auth=False
    )
    return status, headers, body.decode()


def monitor_running(root):
    preview = LivePreview(root)
    root.mkdir(parents=True, exist_ok=True)
    ok, jpeg = cv2.imencode(".jpg", scene_frame())
    assert ok
    preview.frame_path.write_bytes(jpeg.tobytes())
    preview.pulse(True)


def test_areas_page_lists_what_can_be_registered(areas_gui):
    html = page(areas_gui.server, login(areas_gui.server))
    for text in ("監視範囲と基準画像", "トレー外の範囲", "サークルの床面"):
        assert text in html
    assert 'src="/assets/areas.js"' in html
    assert "未登録" in html
    sub = re.search(r'aria-label="管理のメニュー".*?</nav>', html, re.S)[0]
    assert re.search(r'href="/manage/areas" aria-current="page">範囲の登録', sub)


def test_frame_comes_from_the_camera_when_monitoring_is_stopped(areas_gui):
    snapshot(areas_gui.server, login(areas_gui.server))
    assert areas_gui.opened == [SECRET_URL]


def test_frame_comes_from_the_monitor_while_it_runs(areas_gui):
    monitor_running(areas_gui.root)
    snapshot(areas_gui.server, login(areas_gui.server))
    assert areas_gui.opened == []


def test_registering_the_outside_area(areas_gui):
    server = areas_gui.server
    admin = login(server, "管理者")
    status, headers, _ = save(server, admin, kind="outside", snapshot=snapshot(server, admin))
    assert (status, headers["Location"]) == (303, f"{PAGE}?saved=outside")
    roi = ROI.load(areas_gui.root / "outside-roi.json")
    assert np.allclose(roi.points, SQUARE)
    entry = server.app.accounts.audit.recent()[0]
    assert (entry.actor, entry.action) == ("管理者", "area.outside")
    html = page(server, admin, f"{PAGE}?saved=outside")
    assert "トレー外の範囲を登録しました" in html
    assert "起動し直す" in html


def test_registering_the_floor_saves_the_background(areas_gui):
    server = areas_gui.server
    admin = login(server)
    status, _, _ = save(server, admin, kind="floor", snapshot=snapshot(server, admin))
    assert status == 303
    layout = ActivityLayout.load(areas_gui.root)
    assert np.allclose(layout.roi.points, SQUARE)
    assert layout.background(areas_gui.root).is_file()


def test_registering_the_tray_area_and_baseline_while_stopped(areas_gui):
    server = areas_gui.server
    admin = login(server)
    shot = snapshot(server, admin)
    status, _, body = save(server, admin, kind="roi", snapshot=shot)
    assert status == 400
    assert "空のトレー" in body
    status, _, _ = save(
        server, admin, kind="roi", snapshot=snapshot(server, admin), confirm_empty="yes"
    )
    assert status == 303
    assert np.allclose(ROI.load(areas_gui.root / "roi.json").points, SQUARE)
    assert (areas_gui.root / "baseline" / "empty.png").is_file()
    assert server.app.accounts.audit.recent()[0].action == "area.roi"


def test_tray_area_is_not_registered_here_while_monitoring(areas_gui):
    server = areas_gui.server
    admin = login(server)
    monitor_running(areas_gui.root)
    status, _, body = save(
        server, admin, kind="roi", snapshot=snapshot(server, admin), confirm_empty="yes"
    )
    assert status == 409
    assert "トレー交換" in body
    assert not (areas_gui.root / "roi.json").exists()


def test_replacing_a_registration_needs_confirmation(areas_gui):
    server = areas_gui.server
    admin = login(server)
    save(server, admin, kind="outside", snapshot=snapshot(server, admin))
    other = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]
    status, _, body = save(
        server, admin, kind="outside", snapshot=snapshot(server, admin), points=json.dumps(other)
    )
    assert status == 400
    assert "登録済み" in body
    assert np.allclose(ROI.load(areas_gui.root / "outside-roi.json").points, SQUARE)
    status, _, _ = save(
        server,
        admin,
        kind="outside",
        snapshot=snapshot(server, admin),
        points=json.dumps(other),
        confirm_replace="yes",
    )
    assert status == 303
    assert np.allclose(ROI.load(areas_gui.root / "outside-roi.json").points, other)
    assert "登録済み" in page(server, admin)


def test_invalid_polygons_and_stale_frames_are_refused(areas_gui):
    server = areas_gui.server
    admin = login(server)
    bowtie = [[0.1, 0.1], [0.9, 0.9], [0.9, 0.1], [0.1, 0.9]]
    status, _, body = save(
        server, admin, kind="outside", snapshot=snapshot(server, admin), points=json.dumps(bowtie)
    )
    assert status == 400
    assert "多角形" in body or "頂点" in body
    status, _, body = save(server, admin, kind="outside", snapshot="0" * 32)
    assert status == 400
    assert "映像を取り直して" in body
    status, _, _ = save(server, admin, kind="other", snapshot=snapshot(server, admin))
    assert status == 400
    assert not (areas_gui.root / "outside-roi.json").exists()
    assert not (areas_gui.root / LAYOUT_FILE).exists()


def test_a_frame_is_used_only_once(areas_gui):
    server = areas_gui.server
    admin = login(server)
    shot = snapshot(server, admin)
    assert save(server, admin, kind="outside", snapshot=shot)[0] == 303
    status, _, _ = save(server, admin, kind="floor", snapshot=shot)
    assert status == 400


def test_camera_failure_does_not_show_the_url(areas_gui, monkeypatch):
    def camera(*_args):
        return nullcontext(SimpleNamespace(read=lambda: None))

    monkeypatch.setattr("poop_detector.commands.context.Camera", camera)
    status, _, body = exchange(
        areas_gui.server, "GET", FRAME_URL, headers=login(areas_gui.server), auth=False
    )
    assert status == 503
    assert "映像を取得できません" in body.decode()
    assert b"192.0.2.10" not in body


def test_tray_tracking_saves_the_tray_reference_too(areas_gui, tmp_path):
    (tmp_path / "config.yaml").write_text(
        f"data_dir: {areas_gui.root}\nfeatures:\n  track_tray: true\n"
    )
    server = areas_gui.server
    admin = login(server)
    # トレーの縁のない場所は、追従の基準にしない。
    corner = json.dumps([[0.0, 0.0], [0.15, 0.0], [0.15, 0.15], [0.0, 0.15]])
    status, _, body = save(
        server,
        admin,
        kind="roi",
        snapshot=snapshot(server, admin),
        points=corner,
        confirm_empty="yes",
    )
    assert status == 400
    assert "トレーの縁" in body
    assert not (areas_gui.root / "roi.json").exists()
    status, _, body = save(
        server,
        admin,
        kind="roi",
        snapshot=snapshot(server, admin),
        points=json.dumps(TRAY),
        confirm_empty="yes",
    )
    assert status == 303, body
    assert (areas_gui.root / "baseline" / "tray-reference.png").is_file()


def test_missing_monitor_frame_is_reported(areas_gui, monkeypatch):
    monkeypatch.setattr("poop_detector.gui.areas_routes.PREVIEW_WAIT_SECONDS", 0.2)
    LivePreview(areas_gui.root).pulse(True)
    status, _, body = exchange(
        areas_gui.server, "GET", FRAME_URL, headers=login(areas_gui.server), auth=False
    )
    assert status == 503
    assert "ライブ映像を取得できません" in body.decode()
    assert areas_gui.opened == []


def test_monitoring_selects_an_area_that_can_be_registered(areas_gui):
    server = areas_gui.server
    admin = login(server)
    assert re.search(r'value="roi" checked>', page(server, admin))
    monitor_running(areas_gui.root)
    html = page(server, admin)
    assert re.search(r'value="roi" disabled>', html)
    assert re.search(r'value="outside" checked>', html)
