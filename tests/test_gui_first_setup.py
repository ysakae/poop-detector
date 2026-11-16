"""管理者向けの初回設定で、登録済みの工程を確認する。"""

import re

import numpy as np
from support import login, request

from poop_detector.detection.roi import ROI
from poop_detector.detection.scene import save_baseline


def test_first_setup_shows_existing_registration(gui, monkeypatch):
    server, store, _ = gui
    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://secret.example")
    status, payload = request(server, "GET", "/manage/setup")
    assert status == 200
    page = payload.decode()
    assert "未登録" in page
    assert "secret.example" not in page
    for path in ("/manage/checks", "/manage/areas", "/manage/invite", "/account#web-push"):
        assert f'href="{path}"' in page

    roi = ROI(0, 0, 1, 1)
    roi.save(store.root / "roi.json")
    save_baseline(store.root / "baseline" / "empty.png", np.zeros((60, 80, 3), np.uint8), roi)
    server.app.preview.pulse(True)
    status, payload = request(server, "GET", "/manage/setup")
    assert status == 200
    assert "登録済み" in payload.decode()
    assert "カメラに接続中" in payload.decode()


def test_first_setup_is_admin_only(gui):
    server, _, _ = gui
    assert request(server, "GET", "/manage/setup", auth=False)[0] == 403
    assert request(server, "GET", "/manage/setup", headers=login(server, role="member"))[0] == 403


def primary_buttons(page):
    """塗りのボタン（secondary・ghost でないボタン）のリンク先。"""
    main = page.split("<main", 1)[1]
    return re.findall(r'<a class="button" href="([^"]+)"', main)


def test_first_setup_fills_only_the_next_step(gui, monkeypatch, tmp_path):
    """1画面に主役は1つ。まだ済んでいない最初の手順のボタンだけを塗りにする。"""
    server, store, _ = gui
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POOP_RTSP_URL", "")

    def page():
        return request(server, "GET", "/manage/setup")[1].decode()

    assert primary_buttons(page()) == ["/manage/settings#connections-title"]
    assert page().count('<span class="chip chip-warn">次の手順</span>') == 1
    assert "済み</span>" not in page()

    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://secret.example")
    assert primary_buttons(page()) == ["/manage/areas"]

    roi = ROI(0, 0, 1, 1)
    roi.save(store.root / "roi.json")
    save_baseline(store.root / "baseline" / "empty.png", np.zeros((60, 80, 3), np.uint8), roi)
    assert primary_buttons(page()) == ["/manage"]

    server.app.preview.pulse(True)
    done = page()
    assert primary_buttons(done) == ["/manage/invite"]
    assert done.count('<span class="chip chip-ok">済み</span>') == 3
    assert done.count('<span class="chip chip-warn">次の手順</span>') == 1
    # 塗りでないボタンも、押せるリンクとして残す。
    for path in ("/manage/checks", "/manage/areas", "/manage", "/account#web-push"):
        assert f'<a class="button secondary" href="{path}"' in done
