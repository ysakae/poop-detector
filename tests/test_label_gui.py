"""実HTTP経由でラベルの保存と画像の公開範囲を確認する。"""

import re
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from support import csrf_token, exchange, request, session_cookie

from poop_detector.web_address import Site


def post(server, fields, token=True):
    if token:
        fields = {**fields, "csrf": csrf_token(server)}
    return request(
        server,
        "POST",
        "/save",
        urlencode(fields),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )


def day_counts(html):
    """記録の画面の1日の回数（大きな数字の部品）を、名前ごとの値にする。"""
    section = html.split('id="daily-stats-title"', 1)[1].split("</form>", 1)[1]
    return dict(re.findall(r'([^<>]+)</span><strong class="stat-value">([^<]+)<span', section)[:3])


def test_records_are_a_timeline_with_icons(gui):
    server, store, event = gui
    store.classify(event, "feces", note="朝")
    html = request(server, "GET", "/records")[1].decode()
    listing = html.split('<ul class="timeline record-list">', 1)[1].split("</ul>", 1)[0]
    assert '<svg class="icon"' in listing
    assert "排便" in listing
    assert "朝" in listing
    assert f"id={event}" in listing
    assert '<table class="data-table"' not in html


def test_gui_lists_image_and_saves_label(gui):
    server, store, event = gui
    status, data = request(server, "GET", "/records/events")
    assert status == 200
    assert "排便だった".encode() in data
    assert event.encode() in data
    assert request(server, "GET", "/image?id=" + event)[0] == 200
    assert (
        post(
            server,
            {"id": event, "label": "true", "start": "2026-11-08T19:00", "end": "2026-11-08T19:01"},
        )[0]
        == 303
    )
    row = store.list()[0]
    assert row["feedback"] == 1
    assert row["actual_start"] == "2026-11-08T19:00:00+09:00"
    assert post(server, {"id": event, "label": "false"})[0] == 303
    assert store.list()[0]["actual_start"] == row["actual_start"]
    assert post(server, {"id": event, "label": "unknown"})[0] == 303
    assert store.list()[0]["feedback"] is None


def test_gui_rejects_invalid_time_and_missing_csrf(gui):
    server, store, event = gui
    assert post(server, {"id": event, "label": "true"}, token=False)[0] == 403
    assert post(server, {"id": event, "label": "true", "start": "不正"})[0] == 400
    assert (
        post(
            server,
            {"id": event, "label": "true", "start": "2026-11-08T19:01", "end": "2026-11-08T19:00"},
        )[0]
        == 400
    )
    assert store.list()[0]["feedback"] is None
    assert request(server, "GET", "/image?id=../../.env")[0] == 404
    assert request(server, "GET", "/.env")[0] == 404
    assert request(server, "GET", "/", headers={"Host": "evil.example"})[0] == 403


def test_gui_missed_and_filter(gui):
    server, store, event = gui
    assert post(server, {"action": "missed", "start": "2026-11-08T19:00"})[0] == 303
    assert store.metrics()["missed"] == 1
    assert post(server, {"id": event, "label": "false"})[0] == 303
    assert event.encode() not in request(server, "GET", "/records/events?filter=unlabeled")[1]
    assert event.encode() in request(server, "GET", "/records/events?filter=false")[1]


def test_gui_urine_and_excretion_log(gui):
    server, store, event = gui
    assert post(server, {"id": event, "label": "urine", "note": "濡れ跡を確認"})[0] == 303
    assert store.records()[0]["excretion_type"] == "urine"
    assert "濡れ跡を確認".encode() in request(server, "GET", "/records")[1]
    assert (
        post(server, {"action": "manual", "type": "feces", "start": "2026-11-08T23:10"})[0] == 303
    )
    assert len(store.records()) == 2
    assert store.metrics()["missed"] == 0
    assert event.encode() in request(server, "GET", "/records/events?filter=urine")[1]
    assert post(server, {"action": "missed", "type": "both", "start": "2026-11-08T23:20"})[0] == 303
    assert store.metrics()["excretion_counts"] == {"feces": 2, "urine": 2, "unknown": 0}
    assert (
        post(server, {"action": "manual", "type": "wrong", "start": "2026-11-08T23:30"})[0] == 400
    )


def test_records_show_daily_counts_and_hourly_statistics_in_japan_time(gui):
    server, store, _ = gui
    store.add_excretion("feces", "2026-11-02T00:00:00+09:00")
    store.add_excretion("urine", "2026-11-01T23:59:59+09:00")
    store.add_excretion("both", "2026-11-07T15:00:00+00:00", missed=True)
    store.add_excretion("urine", "2026-11-08T00:59:59+09:00", location="outside")
    store.add_excretion("unspecified", "2026-11-08T08:00:00+09:00")
    store.add_excretion("both", "2026-11-08T15:00:00+00:00")

    status, body = request(server, "GET", "/records?date=2026-11-08&days=7")
    assert status == 200
    html = body.decode()
    assert day_counts(html) == {"排便": "1", "排尿": "2", "種類不明": "1"}
    assert "2026-11-02〜2026-11-08（7日間・記録あり2日）" in html
    hourly = html.split('id="hourly-stats"', 1)[1].split("</table>", 1)[0]
    assert re.search(r"00:00〜00:59</th>.*?</meter> 2件.*?</meter> 2件", hourly)
    assert re.search(r"08:00〜08:59</th>.*?</meter> 0件.*?</meter> 0件", hourly)


def test_records_stats_follow_confirmed_start_or_fall_back_to_record_time(gui):
    server, store, event = gui
    with store.connect() as db:
        db.execute("UPDATE events SET timestamp=? WHERE id=?", ("2026-11-07T15:25:00+00:00", event))
    url = "/records?date=2026-11-08&days=7"
    assert day_counts(request(server, "GET", url)[1].decode())["排尿"] == "0"
    store.classify(event, "urine", start=None)
    assert day_counts(request(server, "GET", url)[1].decode())["排尿"] == "1"
    store.classify(event, "urine", start="2026-11-07T23:55:00+09:00")
    assert day_counts(request(server, "GET", url)[1].decode())["排尿"] == "0"
    assert request(server, "GET", "/records?date=invalid&days=-1")[0] == 200


# --- 閲覧の認証 ---

PROTECTED_PATHS = (
    "/",
    "/records",
    "/clips",
    "/manage/storage",
    "/clips/capacity",
    "/activity",
    "/activity/background?id=x",
    "/live/frame",
    "/live/stream",
    "/live/status",
    "/live/assessment",
    "/clip-video?id=x",
    "/assets/live.js",
)


def test_gui_rejects_get_without_session(gui):
    server, _, event = gui
    image = request(server, "GET", "/image?id=" + event)[1]
    for path in (*PROTECTED_PATHS, "/image?id=" + event):
        for headers in ({}, {"Cookie": "poop_detector_session=wrong"}):
            status, data = request(server, "GET", path, headers=headers, auth=False)
            assert status == 403, path
            assert image not in data
            assert b"csrf" not in data


def test_gui_rejects_post_without_session_even_with_csrf(gui):
    server, store, event = gui
    page = request(server, "GET", "/records/events")[1].decode()
    csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
    body = urlencode({"id": event, "label": "false", "csrf": csrf})
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    assert request(server, "POST", "/save", body, headers, auth=False)[0] == 403
    assert store.list()[0]["feedback"] is None


def test_gui_accepts_session_among_other_cookies_and_hides_referrer(gui):
    server, _, _ = gui
    session = session_cookie(server)["Cookie"]
    cookie = f"other=1; poop_detector_session=old; {session}"
    status, headers, _ = exchange(server, "GET", "/", headers={"Cookie": cookie}, auth=False)
    assert status == 200
    # no-referrer だと、ブラウザーは同じサイトのフォームの送信にも Origin: null を付け、
    # Origin の検査で保存が断られる。ほかのサイトに URL を渡さない same-origin にする。
    assert headers["Referrer-Policy"] == "same-origin"


class FakeServer:
    server_port = 8765

    def __init__(self, has_users=True, public_url=""):
        self.site = Site(8765, public_url)
        self.app = SimpleNamespace(
            accounts=SimpleNamespace(users=SimpleNamespace(list=lambda: [1] if has_users else []))
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def serve_forever(self):
        raise KeyboardInterrupt


@pytest.fixture
def launch(monkeypatch, capsys):
    from poop_detector.gui import server as label_gui

    opened = []

    def run(open_browser, *, has_users=True, browser_ok=True, public_url=""):
        monkeypatch.setattr(
            label_gui, "make_server", lambda *a, **k: FakeServer(has_users, public_url)
        )
        monkeypatch.setattr(
            label_gui.webbrowser, "open", lambda url: opened.append(url) or browser_ok
        )
        label_gui.serve("data", open_browser=open_browser)
        return capsys.readouterr().out

    return run, opened


def test_serve_opens_localhost_for_passkeys(launch):
    """パスキーは IP アドレスでは使えないので、localhost で開く。"""
    run, opened = launch
    out = run(True)
    assert opened == ["http://localhost:8765/"]
    assert "http://localhost:8765/" in out
    assert "GUIを終了しました" in out


def test_serve_opens_the_public_url_when_one_is_configured(launch):
    """パスキーは登録したドメインでだけ使えるので、外部の URL があればそちらを開く。"""
    run, opened = launch
    out = run(True, public_url="https://toilet.example.ts.net")
    assert opened == ["https://toilet.example.ts.net/"]
    assert "外から開くとき: https://toilet.example.ts.net/（ブラウザーで開きました）" in out
    # 同じマシンから開く URL も案内する（ブラウザーで開いたのは外部の URL だけ）。
    assert "GUI: http://localhost:8765/\n" in out


def test_serve_without_users_explains_the_first_registration(launch):
    run, opened = launch
    out = run(False, has_users=False)
    assert opened == []
    assert "users invite" in out
