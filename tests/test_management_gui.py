"""管理画面の交換操作と、変更要求のアクセス制御を確認する。"""

from support import request, submit

from poop_detector.detection.roi import ROI
from poop_detector.features.tray_registration import TrayRegistration


def test_management_tray_setup_from_the_browser(gui):
    server, _, _ = gui
    registration = TrayRegistration(server.app.root)
    status, body = request(server, "GET", "/manage")
    assert status == 200
    assert "トレー交換を開始".encode() in body

    assert submit(server, "/manage/tray", {"action": "begin"}, page="/manage")[0] == 303
    roi = ROI(0, 0, 1, 1)
    registration.poll(None, roi, track_tray=False)
    status, body = request(server, "GET", "/manage")
    assert status == 200
    assert b'id="tray-preview"' in body

    polygon = '{"mode":"mask","points":[[0.1,0.1],[0.8,0.2],[0.7,0.8],[0.2,0.7]]}'
    assert (
        submit(server, "/manage/tray", {"action": "apply", "roi": polygon}, page="/manage")[0]
        == 400
    )
    assert registration.status()["phase"] == "paused"
    assert (
        submit(
            server,
            "/manage/tray",
            {"action": "apply", "roi": polygon, "confirm_empty": "yes"},
            page="/manage",
        )[0]
        == 303
    )
    assert registration.status()["roi"]["mode"] == "mask"

    assert submit(server, "/manage/tray", {"action": "cancel"}, page="/manage")[0] == 303
    registration.poll(None, roi, track_tray=False)
    assert "取り消しました".encode() in request(server, "GET", "/manage")[1]


def test_management_changes_require_a_session_and_csrf_token(gui):
    server, _, _ = gui
    assert request(server, "GET", "/manage", auth=False)[0] == 403
    for auth in (False, True):
        assert request(server, "POST", "/manage/tray", "action=begin", auth=auth)[0] == 403
    assert TrayRegistration(server.app.root).status()["phase"] == "idle"


def test_management_lists_recent_outages(gui):
    """稼働状況に、直近の監視の停止とカメラの不調を新しい順に出す（#91）。"""
    from poop_detector.features.outages import OutageLog
    from poop_detector.records.database import events_database

    server, _, _ = gui
    body = request(server, "GET", "/manage")[1].decode()
    assert "記録された停止はありません" in body
    log = OutageLog(events_database(server.app.root))
    camera, _ = log.begin("camera", "2026-11-12T00:00:00+00:00")
    log.mark_notified(camera.id, "2026-11-12T00:05:00+00:00")
    log.end(camera.id, "2026-11-12T01:05:00+00:00")
    log.begin("monitor", "2026-11-12T02:00:00+00:00")
    body = request(server, "GET", "/manage")[1].decode()
    history = body[body.index("直近の停止") :]
    assert history.index("監視") < history.index("カメラ")
    assert "2026/11/12 09:00:00" in history
    assert "約1時間5分" in history
    assert "継続中" in history
    assert "送信済み" in history
    assert "未送信" in history
