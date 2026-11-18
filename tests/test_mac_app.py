"""Mac アプリからローカルで手動記録を始める。"""

from urllib.parse import parse_qs, urlsplit

from poop_detector import mac_app
from poop_detector.gui.server import make_server


def test_mac_app_opens_one_time_admin_invitation(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(mac_app.webbrowser, "open", lambda url: opened.append(url) or True)

    with make_server(tmp_path, 0, manual_only=True) as server:
        with mac_app.open_record_book(server) as show:
            show()
            url = urlsplit(opened[0])
            assert url.netloc == f"localhost:{server.server_port}"
            assert url.path == "/enroll"
            code = parse_qs(url.query)["code"][0]
            invitation = server.app.accounts.enrollments.peek(code)
            assert invitation is not None
            assert invitation.role == "admin"
        assert server.app.accounts.enrollments.peek(code) is None


def test_mac_app_reopens_record_book_for_existing_account(tmp_path, monkeypatch):
    with make_server(tmp_path, 0) as server:
        account = server.app.accounts.users.create("飼い主", "admin")
        server.app.store.add_excretion("urine", "2026-11-18T10:00:00+09:00")

    opened = []
    monkeypatch.setattr(mac_app.webbrowser, "open", lambda url: opened.append(url) or True)
    with (
        make_server(tmp_path, 0, manual_only=True) as server,
        mac_app.open_record_book(server) as show,
    ):
        show()
        assert opened == [f"http://localhost:{server.server_port}/"]
        assert server.app.accounts.users.get(account) is not None
        assert len(server.app.store.records()) == 1


def test_mac_app_session_cookie_has_its_own_name():
    from poop_detector.gui.access import session_cookie, session_ids

    cookie = session_cookie("mac-session", name="poop_detector_mac_session")
    assert session_ids(cookie) == []
    assert session_ids(cookie, name="poop_detector_mac_session") == ["mac-session"]


def test_mac_app_can_open_the_browser_again_after_registration(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(mac_app.webbrowser, "open", lambda url: opened.append(url) or True)

    with (
        make_server(tmp_path, 0, manual_only=True) as server,
        mac_app.open_record_book(server) as show,
    ):
        show()
        assert "/enroll?code=" in opened[0]
        code = parse_qs(urlsplit(opened[0]).query)["code"][0]

        server.app.accounts.users.create("飼い主", "admin")
        show()
        assert opened[1] == f"http://localhost:{server.server_port}/"
        assert server.app.accounts.enrollments.peek(code) is None


def test_mac_app_window_serves_records_and_stops_with_the_window(tmp_path, monkeypatch):
    from contextlib import closing
    from http.client import HTTPConnection

    opened = []
    monkeypatch.setattr(mac_app, "MAC_PORT", 0)
    monkeypatch.setattr(mac_app.webbrowser, "open", lambda url: opened.append(url) or True)

    def open_record_window(show):
        show()
        url = urlsplit(opened[0])
        with closing(HTTPConnection("localhost", url.port, timeout=5)) as connection:
            connection.request("GET", f"{url.path}?{url.query}")
            response = connection.getresponse()
            assert response.status == 200
            assert "パスキーを登録する" in response.read().decode()

    monkeypatch.setattr(mac_app, "_show_window", open_record_window)
    mac_app.run(tmp_path)
    assert len(opened) == 1
