"""管理 › 設定の画面（#88）を検証する。"""

import re
import threading
from urllib.parse import urlencode

import numpy as np
import pytest
from support import exchange, login

from poop_detector.config import Config
from poop_detector.records.events import Store

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
PAGE = "/manage/settings"
SECRET_URL = "rtsp://user:secret-password@192.0.2.10/stream"


@pytest.fixture
def settings_gui(tmp_path, monkeypatch):
    """設定ファイルの場所を渡して起動した GUI。作業ディレクトリの .env に接続情報を置く。"""
    from poop_detector.gui.server import make_server

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"POOP_RTSP_URL={SECRET_URL}\n")
    config_path = tmp_path / "config" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("# 手で書いたコメント\ndetection:\n  threshold: 30\n")
    Store(tmp_path).record(np.full((50, 50, 3), 150, np.uint8), 0.1, None, 1)
    server = make_server(tmp_path, 0, config_path=config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, config_path
    server.shutdown()
    server.server_close()
    thread.join()


def page(server, cookie, path=PAGE):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200
    return body.decode()


def form_fields(html):
    """画面のフォームに入っている値（送信されるもの）を取り出す。"""
    fields = dict(re.findall(r'name="([a-z_]+)" value="([^"]*)"', html))
    checked = set(re.findall(r'name="([a-z_]+)" value="true" checked', html))
    for name in re.findall(r'type="checkbox" name="([a-z_]+)"', html):
        if name not in checked:
            fields.pop(name, None)
    for name, value in re.findall(
        r'<select name="([a-z_]+)">.*?<option value="([^"]*)" selected', html, re.S
    ):
        fields[name] = value
    return fields


def post(server, cookie, fields):
    body = urlencode(fields)
    return exchange(server, "POST", PAGE, body, {**FORM, **cookie}, auth=False)


def test_settings_page_shows_every_section_and_hides_secrets(settings_gui):
    server, _ = settings_gui
    html = page(server, login(server))
    for heading in ("検知", "カメラ", "動画", "通知", "犬の検出", "動画の自動判定", "機能"):
        assert f"<h3>{heading}</h3>" in html
    assert 'name="threshold" value="30"' in html
    # 外からの公開に関わる項目は表示だけ。
    assert 'name="gui_public_url"' not in html
    assert "gui.public_url" in html
    # 接続情報は「設定あり・未設定」だけを出す。
    assert "secret-password" not in html
    assert "192.0.2.10" not in html
    assert re.search(r"カメラの RTSP URL</th><td>設定あり", html)
    assert re.search(r"ntfy の通知トピック</th><td>未設定", html)
    # サーバーのディレクトリの場所は出さない。
    assert str(settings_gui[1].parent) not in html
    assert "config.yaml" in html


def test_changes_are_previewed_before_saving(settings_gui):
    server, path = settings_gui
    admin = login(server, "管理者")
    fields = {**form_fields(page(server, admin)), "threshold": "25", "action": "preview"}
    status, _, body = post(server, admin, fields)
    html = body.decode()
    assert status == 200
    assert "detection.threshold" in html
    assert re.search(r"<td>30</td><td>25</td>", html)
    assert 'value="save"' in html
    # 確認しただけでは書き換えない。
    assert Config.load(path).threshold == 30


def test_saving_writes_the_file_and_the_audit_log(settings_gui):
    server, path = settings_gui
    admin = login(server, "管理者")
    fields = {**form_fields(page(server, admin)), "threshold": "25", "action": "save"}
    status, headers, _ = post(server, admin, fields)
    assert (status, headers["Location"]) == (303, f"{PAGE}?saved=1")
    assert Config.load(path).threshold == 25
    assert "# 手で書いたコメント" in path.read_text()
    entry = server.app.accounts.audit.recent()[0]
    assert (entry.actor, entry.action) == ("管理者", "settings.save")
    assert "detection.threshold: 30 → 25" in entry.detail
    html = page(server, admin, f"{PAGE}?saved=1")
    assert "保存しました" in html
    assert "起動し直す" in html


def test_invalid_values_are_refused_with_the_range(settings_gui):
    server, path = settings_gui
    admin = login(server)
    fields = {**form_fields(page(server, admin)), "threshold": "999", "action": "preview"}
    status, _, body = post(server, admin, fields)
    assert status == 400
    assert "detection.threshold は1〜255 の整数" in body.decode()
    # 入力した値を残して表示し直す。
    assert 'name="threshold" value="999"' in body.decode()
    assert Config.load(path).threshold == 30


def test_saving_without_changes_says_so(settings_gui):
    server, _ = settings_gui
    admin = login(server)
    fields = {**form_fields(page(server, admin)), "action": "preview"}
    status, _, body = post(server, admin, fields)
    assert status == 200
    assert "変更はありません" in body.decode()


def test_saving_after_someone_else_edited_the_file_is_refused(settings_gui):
    server, path = settings_gui
    admin = login(server)
    fields = {**form_fields(page(server, admin)), "threshold": "25", "action": "save"}
    path.write_text("detection:\n  threshold: 40\n")
    status, _, body = post(server, admin, fields)
    assert status == 409
    assert "ほかで書き換えられました" in body.decode()
    assert Config.load(path).threshold == 40


def test_previous_settings_can_be_restored(settings_gui):
    server, path = settings_gui
    admin = login(server, "管理者")
    assert "直前の設定に戻す" not in page(server, admin)
    fields = {**form_fields(page(server, admin)), "threshold": "25", "action": "save"}
    post(server, admin, fields)
    html = page(server, admin)
    assert "直前の設定に戻す" in html
    fields = {**form_fields(html), "action": "restore", "confirm_restore": "1"}
    status, headers, _ = post(server, admin, fields)
    assert (status, headers["Location"]) == (303, f"{PAGE}?restored=1")
    assert Config.load(path).threshold == 30
    assert server.app.accounts.audit.recent()[0].action == "settings.restore"


def test_settings_page_without_a_config_path_is_read_only(gui):
    server, _, _ = gui
    html = page(server, login(server))
    assert "設定ファイルの場所が分からない" in html
    assert 'value="preview"' not in html


def test_management_menu_lists_settings(settings_gui):
    server, _ = settings_gui
    html = page(server, login(server))
    sub = re.search(r'aria-label="管理のメニュー".*?</nav>', html, re.S)[0]
    assert re.search(r'href="/manage/settings" aria-current="page">設定', sub)


def test_unreadable_settings_file_explains_why(settings_gui):
    server, path = settings_gui
    path.write_text("detection:\n  threshold: [\n")
    status, _, body = exchange(server, "GET", PAGE, headers=login(server), auth=False)
    assert status == 400
    assert "設定ファイルを読めません" in body.decode()


def test_member_cannot_open_settings(settings_gui):
    server, path = settings_gui
    member = login(server, role="member")
    assert exchange(server, "GET", PAGE, headers=member, auth=False)[0] == 403
    assert Config.load(path).threshold == 30
