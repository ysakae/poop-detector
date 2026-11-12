"""管理 › 確認と診断（通知のテスト送信、カメラの確認、診断情報。#88）を検証する。"""

import re
import threading
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import urlencode

import numpy as np
import pytest
from support import exchange, login

from poop_detector.capture.live_preview import LivePreview

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
PAGE = "/manage/checks"
SECRET_URL = "rtsp://user:secret-password@192.0.2.10/stream"
SECRET_TOPIC = "secret-topic-123"


@pytest.fixture
def checks_gui(tmp_path, monkeypatch):
    """作業ディレクトリに .env と設定ファイルを置いて起動した GUI。"""
    from poop_detector.gui.server import make_server

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"POOP_RTSP_URL={SECRET_URL}\nPOOP_NTFY_TOPIC={SECRET_TOPIC}\n")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"data_dir: {tmp_path / 'data'}\npet_name: ポチ\n")
    server = make_server(tmp_path / "data", 0, config_path=config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


class FakeNotifier:
    def __init__(self, result=True):
        self.result = result
        self.sent = []

    def send_test(self, *, early_alert=False):
        self.sent.append(early_alert)
        return self.result


def page(server, cookie, path=PAGE):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def post(server, cookie, fields):
    token = re.search(r'name="csrf" value="([^"]+)"', page(server, cookie))[1]
    body = urlencode({**fields, "csrf": token})
    status, _, data = exchange(server, "POST", PAGE, body, {**FORM, **cookie}, auth=False)
    return status, data.decode()


def use_notifier(monkeypatch, notifier, uses_ntfy=True):
    calls = []

    def build(config, settings, *, sends):
        calls.append((config.pet_name, sends))
        return notifier, uses_ntfy

    monkeypatch.setattr("poop_detector.gui.checks_routes.build_notifier", build)
    return calls


def test_checks_page_offers_the_three_checks(checks_gui):
    html = page(checks_gui, login(checks_gui))
    for heading in ("通知のテスト送信", "カメラの確認", "診断情報"):
        assert f">{heading}</h3>" in html
    assert 'value="notify"' in html
    assert 'value="camera"' in html
    assert f'href="{PAGE}?diagnose=1"' in html
    sub = re.search(r'aria-label="管理のメニュー".*?</nav>', html, re.S)[0]
    assert re.search(r'href="/manage/checks" aria-current="page">確認と診断', sub)


def test_test_notification_is_sent_and_audited(checks_gui, monkeypatch):
    notifier = FakeNotifier()
    calls = use_notifier(monkeypatch, notifier)
    admin = login(checks_gui, "管理者")
    status, html = post(checks_gui, admin, {"action": "notify"})
    assert status == 200
    assert "ntfy が通知を受け付けました" in html
    assert notifier.sent == [False]
    # 設定ファイルの値（犬の名前）で通知を組み立て、実際に送る通知として扱う。
    assert calls == [("ポチ", True)]
    assert SECRET_TOPIC not in html
    entry = checks_gui.app.accounts.audit.recent()[0]
    assert (entry.actor, entry.action, entry.detail) == ("管理者", "check.notification", "トイレ")


def test_early_alert_test_needs_its_topic(checks_gui, monkeypatch):
    notifier = FakeNotifier()
    use_notifier(monkeypatch, notifier)
    status, html = post(checks_gui, login(checks_gui), {"action": "notify_early"})
    assert status == 400
    assert "POOP_NTFY_EARLY_TOPIC" in html
    assert notifier.sent == []


def test_failed_notification_says_so(checks_gui, monkeypatch):
    use_notifier(monkeypatch, FakeNotifier(result=False))
    status, html = post(checks_gui, login(checks_gui), {"action": "notify"})
    assert status == 502
    assert "送信に失敗しました" in html


def test_camera_check_reads_one_frame_when_monitoring_is_stopped(checks_gui, monkeypatch):
    frame = np.zeros((20, 30, 3), np.uint8)
    opened = []

    def camera(source, *_args):
        opened.append(source)
        return nullcontext(SimpleNamespace(read=lambda: frame))

    monkeypatch.setattr("poop_detector.commands.context.Camera", camera)
    admin = login(checks_gui, "管理者")
    status, html = post(checks_gui, admin, {"action": "camera"})
    assert status == 200
    assert "映像を取得できました（30×20）" in html
    assert opened == [SECRET_URL]
    assert "secret-password" not in html
    assert checks_gui.app.accounts.audit.recent()[0].action == "check.camera"


def test_camera_check_reports_failure_without_the_url(checks_gui, monkeypatch):
    def camera(*_args):
        return nullcontext(SimpleNamespace(read=lambda: None))

    monkeypatch.setattr("poop_detector.commands.context.Camera", camera)
    status, html = post(checks_gui, login(checks_gui), {"action": "camera"})
    assert status == 502
    assert "映像を取得できません" in html
    assert "192.0.2.10" not in html


def test_camera_check_uses_the_running_monitor_instead_of_a_second_connection(
    checks_gui, monkeypatch
):
    def camera(*_args):
        raise AssertionError("監視中にカメラへ2本目の接続を開いた")

    monkeypatch.setattr("poop_detector.commands.context.Camera", camera)
    LivePreview(checks_gui.app.root).pulse(True)
    status, html = post(checks_gui, login(checks_gui), {"action": "camera"})
    assert status == 200
    assert "監視がカメラにつながっています" in html


def test_diagnostics_are_shown_without_secrets(checks_gui, monkeypatch):
    monkeypatch.setattr(
        "poop_detector.commands.diagnose.programs_summary", lambda: {"ffmpeg": "見つかりました"}
    )
    html = page(checks_gui, login(checks_gui), f"{PAGE}?diagnose=1")
    report = re.search(r"<pre[^>]*>(.*?)</pre>", html, re.S)[1]
    assert "poop_detector:" in report
    assert "rtsp_url: 設定あり" in report
    assert SECRET_TOPIC not in html
    assert "secret-password" not in html
    assert "ポチ" not in report


def test_unknown_action_is_refused(checks_gui):
    status, _ = post(checks_gui, login(checks_gui), {"action": "other"})
    assert status == 400


def test_members_cannot_run_checks(checks_gui, monkeypatch):
    notifier = FakeNotifier()
    use_notifier(monkeypatch, notifier)
    member = login(checks_gui, role="member")
    assert exchange(checks_gui, "GET", PAGE, headers=member, auth=False)[0] == 403
    assert notifier.sent == []
