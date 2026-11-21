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
from poop_detector.gui.assets import asset_url

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
    assert f'src="{asset_url("areas.js")}"' in html
    assert "未登録" in html
    sub = re.search(r'aria-label="管理の中の画面".*?</nav>', html, re.S)[0]
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
    assert re.search(r'value="floor" checked>', html)


def card(html, kind):
    return re.search(rf'<article class="area-card" id="area-{kind}">.*?</article>', html, re.S)[0]


def test_each_area_says_what_it_is_for(areas_gui):
    html = page(areas_gui.server, login(areas_gui.server))
    # 並びは、必須のトレー、床面、任意のトレー外の範囲。
    assert re.findall(r'<article class="area-card" id="area-(\w+)">', html) == [
        "roi",
        "floor",
        "outside",
    ]
    assert "排泄の検知" in card(html, "roi")
    assert "行動マップ" in card(html, "floor")
    assert "睡眠" in card(html, "floor")
    outside = card(html, "outside")
    assert "任意" in outside
    # 登録していなければ、床面を使う。トレー外検知が止まっていることも示す。
    assert "床面と同じ範囲を使います" in outside
    assert "トレー外検知はオフです" in outside


def test_tray_card_leads_to_the_tray_exchange_while_monitoring(areas_gui):
    server = areas_gui.server
    admin = login(server)
    assert 'href="/manage/areas/tray"' not in card(page(server, admin), "roi")
    monitor_running(areas_gui.root)
    tray = card(page(server, admin), "roi")
    assert re.search(r'<a class="button[^"]*" href="/manage/areas/tray">', tray)


def test_custom_outside_area_can_go_back_to_the_floor(areas_gui):
    server = areas_gui.server
    admin = login(server, "管理者")
    save(server, admin, kind="outside", snapshot=snapshot(server, admin))
    html = page(server, admin)
    outside = card(html, "outside")
    assert "床面と別の範囲を登録しています" in outside
    token = re.search(r'name="csrf" value="([^"]+)"', html)[1]
    body = urlencode({"csrf": token, "confirm_clear": "yes"})
    status, headers, _ = exchange(
        server, "POST", f"{PAGE}/outside/clear", body, {**FORM, **admin}, auth=False
    )
    assert (status, headers["Location"]) == (303, f"{PAGE}?cleared=outside")
    assert not (areas_gui.root / "outside-roi.json").exists()
    assert server.app.accounts.audit.recent()[0].action == "area.outside.clear"
    assert "床面と同じ範囲を使います" in card(page(server, admin), "outside")


def test_clearing_the_outside_area_needs_confirmation(areas_gui):
    server = areas_gui.server
    admin = login(server)
    save(server, admin, kind="outside", snapshot=snapshot(server, admin))
    token = re.search(r'name="csrf" value="([^"]+)"', page(server, admin))[1]
    status, _, _ = exchange(
        server,
        "POST",
        f"{PAGE}/outside/clear",
        urlencode({"csrf": token}),
        {**FORM, **admin},
        auth=False,
    )
    assert status == 400
    assert (areas_gui.root / "outside-roi.json").exists()


def test_each_card_can_show_its_area_on_the_frame(areas_gui):
    html = page(areas_gui.server, login(areas_gui.server))
    for kind in ("roi", "floor", "outside"):
        button = re.search(
            rf'<button[^>]*data-area-show="{kind}"[^>]*>([^<]*)</button>', card(html, kind)
        )
        assert button, kind
        assert 'type="button"' in button[0]
        assert 'aria-pressed="false"' in button[0]
        assert button[1] == "映像で範囲を見る"
    # 監視中でトレーを登録できないときも、登録済みの範囲は見られる。
    monitor_running(areas_gui.root)
    assert 'data-area-show="roi"' in card(page(areas_gui.server, login(areas_gui.server)), "roi")


def test_script_shows_the_chosen_area_and_the_effective_outside_area():
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1] / "src/poop_detector/gui/static/areas.js"
    ).read_text()
    # カードのボタンで見る範囲を選び、映像がまだなければ取りにいく。
    for needle in ("data-area-show", "aria-pressed", "capture()"):
        assert needle in script
    # トレー外の範囲を登録していなければ、代わりに見張る床面を示す。
    assert "existing.floor" in script
