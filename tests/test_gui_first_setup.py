"""管理者向けの初回設定で、登録済みの工程を確認する。"""

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
