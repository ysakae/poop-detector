"""実HTTP経由でラベルの保存と画像の公開範囲を確認する。"""

import re
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


def test_gui_lists_image_and_saves_label(gui):
    server, store, event = gui
    status, data = request(server, "GET", "/")
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
    assert event.encode() not in request(server, "GET", "/?filter=unlabeled")[1]
    assert event.encode() in request(server, "GET", "/?filter=false")[1]


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
    assert event.encode() in request(server, "GET", "/?filter=urine")[1]
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
    assert "排便 1件 ／ 排尿 2件 ／ 種類不明 1件" in html
    assert "2026-11-02〜2026-11-08（7日間・記録あり2日）" in html
    hourly = html.split('id="hourly-stats"', 1)[1].split("</table>", 1)[0]
    assert re.search(r"00:00〜00:59</th>.*?</meter> 2件.*?</meter> 2件", hourly)
    assert re.search(r"08:00〜08:59</th>.*?</meter> 0件.*?</meter> 0件", hourly)


def test_records_stats_follow_confirmed_start_or_fall_back_to_record_time(gui):
    server, store, event = gui
    with store.connect() as db:
        db.execute("UPDATE events SET timestamp=? WHERE id=?", ("2026-11-07T15:25:00+00:00", event))
    url = "/records?date=2026-11-08&days=7"
    assert "排便 0件 ／ 排尿 0件".encode() in request(server, "GET", url)[1]
    store.classify(event, "urine", start=None)
    assert "排便 0件 ／ 排尿 1件".encode() in request(server, "GET", url)[1]
    store.classify(event, "urine", start="2026-11-07T23:55:00+09:00")
    assert "排便 0件 ／ 排尿 0件".encode() in request(server, "GET", url)[1]
    assert request(server, "GET", "/records?date=invalid&days=-1")[0] == 200


# --- 閲覧の認証 ---

PROTECTED_PATHS = (
    "/",
    "/records",
    "/clips",
    "/clips/storage",
    "/clips/capacity",
    "/activity",
    "/activity/background?id=x",
    "/live/frame",
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
    page = request(server, "GET", "/")[1].decode()
    csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
    body = urlencode({"id": event, "label": "false", "csrf": csrf})
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    assert request(server, "POST", "/save", body, headers, auth=False)[0] == 403
    assert store.list()[0]["feedback"] is None


def test_token_url_moves_token_into_strict_http_only_cookie(gui):
    server, _, _ = gui
    path = f"/records?token={server.access_token}&days=7"
    status, headers, _ = exchange(server, "GET", path, auth=False)
    assert status == 303
    assert headers["Location"] == "/records?days=7"
    cookie = headers["Set-Cookie"]
    assert cookie.startswith("poop_detector_session=")
    assert server.access_token not in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    name, value = cookie.split(";", 1)[0].split("=", 1)
    status, data = request(
        server, "GET", "/records", headers={"Cookie": f"{name}={value}"}, auth=False
    )
    assert status == 200
    assert "記録".encode() in data


def test_token_url_rejects_wrong_token_and_keeps_redirect_local(gui):
    server, _, _ = gui
    status, headers, _ = exchange(server, "GET", "/?token=wrong", auth=False)
    assert status == 403
    assert "Set-Cookie" not in headers
    for path in ("//evil.example/", "/\\evil.example/"):
        status, headers, _ = exchange(
            server, "GET", f"{path}?token={server.access_token}", auth=False
        )
        assert status == 303
        location = headers["Location"]
        assert location.startswith("/")
        assert not location.startswith(("//", "/\\"))


def test_redirect_without_token_never_leaves_the_gui():
    from poop_detector.gui.access import redirect_without_token

    assert redirect_without_token("//evil.example/", "token=t") == "/"
    assert redirect_without_token("/\\evil.example/", "token=t") == "/"
    assert redirect_without_token("evil", "token=t") == "/"
    assert redirect_without_token("/clips", "token=t&id=a%20b") == "/clips?id=a+b"


def test_gui_accepts_session_among_other_cookies_and_hides_referrer(gui):
    server, _, _ = gui
    session = session_cookie(server)["Cookie"]
    cookie = f"other=1; poop_detector_session=old; {session}"
    status, headers, _ = exchange(server, "GET", "/", headers={"Cookie": cookie}, auth=False)
    assert status == 200
    assert headers["Referrer-Policy"] == "no-referrer"


class FakeServer:
    server_port = 8765
    access_token = "secret-token"
    site = Site(8765)

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
    monkeypatch.setattr(label_gui, "make_server", lambda *a, **k: FakeServer())

    def run(open_browser, *, tty, browser_ok=True):
        monkeypatch.setattr(
            label_gui.webbrowser, "open", lambda url: opened.append(url) or browser_ok
        )
        monkeypatch.setattr(label_gui.sys.stdout, "isatty", lambda: tty)
        label_gui.serve("data", open_browser=open_browser)
        return capsys.readouterr().out

    return run, opened


def test_serve_opens_browser_with_token_and_keeps_it_out_of_logs(launch):
    run, opened = launch
    out = run(True, tty=False)
    assert opened == ["http://127.0.0.1:8765/?token=secret-token"]
    assert "secret-token" not in out
    assert "http://127.0.0.1:8765/" in out


def test_serve_prints_token_url_only_to_terminal(launch):
    run, opened = launch
    assert "http://127.0.0.1:8765/?token=secret-token" in run(False, tty=True)
    assert opened == []
    out = run(False, tty=False)
    assert "secret-token" not in out
    assert "端末" in out
    assert "http://127.0.0.1:8765/?token=secret-token" in run(True, tty=True, browser_ok=False)
