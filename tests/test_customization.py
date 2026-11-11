"""犬の名前・タイムゾーン・ntfy サーバーを設定で変えられることを検証する。"""

import http.client
import json
import os
import threading
import time
from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import pytest

from poop_detector.config import Config, apply_timezone, load_connection_settings
from poop_detector.main import main
from poop_detector.notifications import MacNotifier, NtfyNotifier


def capture_ntfy():
    sent = []

    def open_request(request, *, timeout):
        sent.append((request.full_url, json.loads(request.data)))
        return nullcontext(SimpleNamespace(status=200))

    return sent, open_request


def capture_osascript():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    return calls, runner


# --- 設定値の検証 ---


def test_config_defaults_have_no_pet_name_and_follow_machine_timezone():
    config = Config()
    assert config.pet_name == ""
    assert config.timezone == ""


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"pet_name": 3}, "文字列で指定"),
        ({"pet_name": "x" * 33}, "32文字以内"),
        ({"pet_name": "ポ\nチ"}, "制御文字"),
        ({"timezone": "Mars/Base"}, "IANAタイムゾーン名"),
        ({"timezone": 9}, "IANAタイムゾーン名"),
    ],
)
def test_config_rejects_invalid_pet_name_and_timezone(values, message):
    with pytest.raises(ValueError, match=message):
        Config(**values)


def test_config_strips_pet_name_and_accepts_iana_timezone():
    config = Config(pet_name="  ポチ  ", timezone="America/New_York")
    assert config.pet_name == "ポチ"
    assert config.timezone == "America/New_York"


# --- 通知の文面 ---


def test_notifications_without_pet_name_do_not_mention_any_name():
    sent, opener = capture_ntfy()
    notifier = NtfyNotifier("family", opener=opener, early_topic="watch")
    assert notifier.send()
    assert notifier.send_early_alert()
    assert notifier.send_test()
    texts = [payload for _, payload in sent]
    assert all("ここちゃん" not in p["title"] + p["message"] for p in texts)
    assert texts[0]["title"] == "トイレの通知"
    assert texts[0]["message"].startswith("トイレに変化を検出しました。")
    assert texts[1]["title"] == "トイレの見守り"
    assert "様子を確認してください" in texts[1]["message"]
    assert texts[2]["message"] == "トイレの通知テストです。"


def test_notifications_use_configured_pet_name():
    sent, opener = capture_ntfy()
    notifier = NtfyNotifier("family", opener=opener, early_topic="watch", pet_name="ポチ")
    notifier.send()
    notifier.send_early_alert()
    notifier.send_test(early_alert=True)
    texts = [payload for _, payload in sent]
    assert texts[0]["title"] == "ポチのトイレ"
    assert texts[0]["message"].startswith("ポチのトイレに変化を検出しました。")
    assert texts[1]["title"] == "ポチの見守り"
    assert "ポチの様子を確認してください" in texts[1]["message"]
    assert texts[2]["message"] == "ポチの見守りの通知テストです。"


def test_mac_notification_escapes_pet_name_for_applescript():
    calls, runner = capture_osascript()
    notifier = MacNotifier(runner=runner, pet_name='ポ"チ\\')
    assert notifier.send()
    script = calls[0][2]
    # 引用符とバックスラッシュがエスケープされ、AppleScript の文字列から抜け出せない。
    assert 'ポ\\"チ\\\\のトイレに変化' in script
    assert script.count('"') - script.count('\\"') == 4


# --- ntfy のサーバー ---


def test_ntfy_posts_to_configured_server():
    sent, opener = capture_ntfy()
    notifier = NtfyNotifier("family", opener=opener, server="https://ntfy.example.com/")
    assert notifier.send()
    assert sent[0][0] == "https://ntfy.example.com"


def test_ntfy_server_defaults_to_ntfy_sh():
    sent, opener = capture_ntfy()
    NtfyNotifier("family", opener=opener).send()
    assert sent[0][0] == "https://ntfy.sh"


@pytest.mark.parametrize(
    ("server", "message"),
    [
        ("ftp://ntfy.example.com", "で始まる"),
        ("https://user:secret@ntfy.example.com", "認証情報"),
        ("https://ntfy.example.com/?topic=x", "認証情報"),
        ("https://", "で始まる"),
        ("ntfy.example.com", "で始まる"),
    ],
)
def test_ntfy_rejects_unsafe_server(server, message):
    with pytest.raises(ValueError, match=message):
        NtfyNotifier("family", server=server)


def test_connection_settings_include_ntfy_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POOP_NTFY_SERVER", raising=False)
    (tmp_path / ".env").write_text("POOP_NTFY_SERVER=https://ntfy.example.com\n")
    assert load_connection_settings()["POOP_NTFY_SERVER"] == "https://ntfy.example.com"


def test_cli_uses_pet_name_and_ntfy_server(tmp_path, monkeypatch):
    sent, opener = capture_ntfy()
    monkeypatch.chdir(tmp_path)
    for key in ("POOP_NTFY_TOPIC", "POOP_NTFY_EARLY_TOPIC", "POOP_NTFY_SERVER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("poop_detector.notifications.urlopen", opener)
    (tmp_path / ".env").write_text(
        "POOP_NTFY_TOPIC=family\nPOOP_NTFY_SERVER=https://ntfy.example.com\n"
    )
    (tmp_path / "config.yaml").write_text("pet_name: ポチ\n")
    assert main(["--config", "config.yaml", "test-notification"]) == 0
    url, payload = sent[0]
    assert url == "https://ntfy.example.com"
    assert payload["message"] == "ポチのトイレの通知テストです。"


# --- タイムゾーン ---


def test_apply_timezone_changes_local_conversions():
    from poop_detector.label_gui import local_time, parse_time
    from poop_detector.record_stats import summarize_records

    apply_timezone("America/New_York")
    assert local_time("2026-11-08T23:30:00+09:00") == "2026/11/08 09:30:00"
    assert parse_time("2026-11-08T09:30:00") == "2026-11-08T09:30:00-05:00"
    rows = [
        {"actual_start": "2026-11-08T23:30:00+09:00", "timestamp": "", "excretion_type": "urine"}
    ]
    daily, hourly, days = summarize_records(rows, date(2026, 11, 8), date(2026, 11, 8))
    assert daily["urine"] == 1
    assert hourly[9]["urine"] == 1
    assert days == 1


def test_day_boundary_follows_local_timezone():
    from poop_detector.record_stats import summarize_records

    # UTC 15:30 は日本時間では翌日 00:30、ニューヨークでは同じ日の 10:30。
    rows = [
        {"actual_start": "2026-11-08T15:30:00+00:00", "timestamp": "", "excretion_type": "feces"}
    ]
    tokyo = summarize_records(rows, date(2026, 11, 9), date(2026, 11, 9))
    assert tokyo[0]["feces"] == 1
    assert tokyo[1][0]["feces"] == 1
    apply_timezone("America/New_York")
    new_york = summarize_records(rows, date(2026, 11, 8), date(2026, 11, 8))
    assert new_york[0]["feces"] == 1
    assert new_york[1][10]["feces"] == 1


def test_activity_map_day_follows_local_timezone():
    from datetime import datetime

    from poop_detector.timezones import local_today

    stamp = datetime.fromisoformat("2026-11-08T15:30:00+00:00")
    assert stamp.astimezone().date() == date(2026, 11, 9)
    apply_timezone("America/New_York")
    assert stamp.astimezone().date() == date(2026, 11, 8)
    assert local_today() == datetime.now().date()


def test_apply_timezone_without_name_keeps_machine_setting():
    before = os.environ.get("TZ")
    apply_timezone("")
    assert os.environ.get("TZ") == before


def test_cli_applies_configured_timezone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("timezone: Europe/London\n")
    assert main(["--config", "config.yaml", "records"]) == 0
    assert os.environ["TZ"] == "Europe/London"
    assert time.tzname[0] in ("GMT", "UTC")


# --- GUI の見出し ---


def gui_page(tmp_path, **kwargs):
    from poop_detector.label_gui import make_server

    server = make_server(tmp_path, 0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        pages = {}
        for path in ("/", "/records", "/clips", "/activity"):
            conn = http.client.HTTPConnection(*server.server_address)
            conn.request(
                "GET",
                path,
                headers={
                    "Host": f"127.0.0.1:{server.server_port}",
                    "Cookie": f"poop_detector_session={server.access_token}",
                },
            )
            pages[path] = conn.getresponse().read().decode()
            conn.close()
        return pages
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_gui_title_omits_name_by_default(tmp_path):
    pages = gui_page(tmp_path)
    for page in pages.values():
        assert "<h1>トイレ記録</h1>" in page
        assert "ここちゃん" not in page


def test_gui_title_uses_escaped_pet_name(tmp_path):
    pages = gui_page(tmp_path, pet_name="<b>ポチ</b>")
    for page in pages.values():
        assert "<h1>&lt;b&gt;ポチ&lt;/b&gt;のトイレ記録</h1>" in page
        assert "<b>ポチ</b>" not in page


def test_gui_shows_timezone_name_instead_of_fixed_japan_time(tmp_path):
    apply_timezone("America/New_York")
    page = gui_page(tmp_path)["/records"]
    assert "日本時間" not in page
    assert "EST" in page or "EDT" in page
