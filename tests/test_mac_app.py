"""Mac アプリからローカルで手動記録を始める。"""

from urllib.parse import parse_qs, urlsplit

from poop_detector import mac_app
from poop_detector.gui.server import make_server


def test_mac_app_opens_one_time_admin_invitation(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(mac_app.webbrowser, "open", lambda url: opened.append(url) or True)

    with make_server(tmp_path, 0, manual_only=True) as server:
        with mac_app.open_record_book(server):
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
    with make_server(tmp_path, 0, manual_only=True) as server, mac_app.open_record_book(server):
        assert opened == [f"http://localhost:{server.server_port}/"]
        assert server.app.accounts.users.get(account) is not None
        assert len(server.app.store.records()) == 1


def test_mac_app_session_cookie_has_its_own_name():
    from poop_detector.gui.access import session_cookie, session_ids

    cookie = session_cookie("mac-session", name="poop_detector_mac_session")
    assert session_ids(cookie) == []
    assert session_ids(cookie, name="poop_detector_mac_session") == ["mac-session"]
