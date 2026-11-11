"""パスキーの登録とログインを、ソフトウェアの認証器と実際の HTTP で通しで検証する。"""

import http.client
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from passkey_client import Client
from soft_authenticator import SoftAuthenticator
from support import exchange

from poop_detector.gui.accounts import Accounts
from poop_detector.gui.server import make_server
from poop_detector.records.database import events_database
from poop_detector.web_address import WebOptions

PUBLIC = "https://toilet.example.com"


@contextmanager
def running(root, **web):
    server = make_server(root, 0, web=WebOptions(**web))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, Accounts(events_database(root))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def invite(accounts, name="家族A", role="admin"):
    return accounts.enrollments.issue(role, name=name, created_by="cli")


def test_enrollment_url_registers_a_passkey_and_logs_in(tmp_path):
    with running(tmp_path) as (server, accounts):
        code = invite(accounts)
        browser = Client(server)
        status, _, page = browser.get(f"/enroll?code={code}")
        assert status == 200
        assert "家族A" in page.decode()
        assert 'data-passkey="register"' in page.decode()

        status, result = browser.register(SoftAuthenticator(), code, label="Mac")
        assert status == 200
        assert result == {"redirect": "/"}
        assert browser.get("/")[0] == 200

        (user,) = accounts.users.list()
        assert (user.name, user.role) == ("家族A", "admin")
        (key,) = accounts.passkeys.for_user(user.id)
        assert (key.rp_id, key.label, key.transports) == ("localhost", "Mac", ["internal"])
        # 登録用 URL は一度しか使えない。
        assert browser.post("/webauthn/register/options", {"code": code})[0] == 400
        assert browser.get(f"/enroll?code={code}")[0] == 400


def test_registered_passkey_logs_in_from_another_browser(tmp_path):
    with running(tmp_path) as (server, accounts):
        authenticator = SoftAuthenticator()
        Client(server).register(authenticator, invite(accounts))

        browser = Client(server)
        assert browser.get("/")[0] == 403
        status, result = browser.login(authenticator, next_path="/records?days=7")
        assert status == 200
        assert result == {"redirect": "/records?days=7"}
        assert browser.get("/records")[0] == 200
        (user,) = accounts.users.list()
        assert accounts.users.get(user.id).last_login is not None
        assert accounts.passkeys.for_user(user.id)[0].sign_count == 1


@pytest.mark.parametrize("next_path", ["//evil.example/", "https://evil.example/", "/\\evil"])
def test_login_never_redirects_outside_the_gui(tmp_path, next_path):
    with running(tmp_path) as (server, accounts):
        authenticator = SoftAuthenticator()
        Client(server).register(authenticator, invite(accounts))
        status, result = Client(server).login(authenticator, next_path=next_path)
        assert status == 200
        assert result == {"redirect": "/"}


def test_login_is_refused_for_unknown_disabled_or_unverified_passkeys(tmp_path):
    with running(tmp_path) as (server, accounts):
        admin = SoftAuthenticator()
        Client(server).register(admin, invite(accounts))
        member = SoftAuthenticator()
        member_browser = Client(server)
        member_browser.register(member, invite(accounts, "家族B", "member"))

        assert Client(server).login(SoftAuthenticator())[0] == 400
        assert Client(server).login(SoftAuthenticator(verify=False))[0] == 400

        accounts.users.disable(accounts.users.by_name("家族B").id)
        assert Client(server).login(member)[0] == 400
        # 無効にしたユーザーの、すでに始まっているセッションも使えない。
        assert member_browser.get("/")[0] == 403


def test_registration_requires_user_verification(tmp_path):
    with running(tmp_path) as (server, accounts):
        code = invite(accounts)
        status, result = Client(server).register(SoftAuthenticator(verify=False), code)
        assert status == 400
        assert "error" in result
        assert accounts.users.list() == []


def test_ceremonies_are_single_use_and_tied_to_the_origin(tmp_path):
    with running(tmp_path) as (server, accounts):
        authenticator = SoftAuthenticator()
        Client(server).register(authenticator, invite(accounts))
        browser = Client(server)
        _, begun = browser.post("/webauthn/login/options", {})
        credential = authenticator.login(begun["publicKey"], browser.origin)
        payload = {"ceremony": begun["ceremony"], "credential": credential}
        assert browser.post("/webauthn/login/verify", payload)[0] == 200
        # 同じやり取りは2回使えない。
        assert browser.post("/webauthn/login/verify", payload)[0] == 400
        # Origin のない要求や、別のオリジンからの要求は受け付けない。
        assert browser.post("/webauthn/login/options", {}, origin=False)[0] == 403
        other = Client(server, origin="https://evil.example")
        assert other.post("/webauthn/login/options", {})[0] == 403


def test_recovery_url_adds_a_passkey_to_the_same_user(tmp_path):
    with running(tmp_path) as (server, accounts):
        Client(server).register(SoftAuthenticator(), invite(accounts))
        (user,) = accounts.users.list()
        code = accounts.enrollments.issue_recovery(user.id, created_by="cli")
        replacement = SoftAuthenticator()
        assert Client(server).register(replacement, code, label="新しい端末")[0] == 200
        assert [u.id for u in accounts.users.list()] == [user.id]
        assert len(accounts.passkeys.for_user(user.id)) == 2
        assert Client(server).login(replacement)[0] == 200


def test_name_taken_after_the_invitation_is_reported(tmp_path):
    with running(tmp_path) as (server, accounts):
        code = invite(accounts, "家族A")
        accounts.users.create("家族A", "admin")
        status, result = Client(server).post("/webauthn/register/options", {"code": code})
        assert status == 400
        assert "家族A" in result["error"]
        # 登録用 URL は使ったことにならない。
        assert accounts.enrollments.peek(code) is not None


def test_passkeys_are_bound_to_the_domain_they_were_registered_on(tmp_path):
    with running(tmp_path, public_url=PUBLIC) as (server, accounts):
        public = Client(server, "toilet.example.com", PUBLIC, https=True)
        authenticator = SoftAuthenticator()
        assert public.register(authenticator, invite(accounts))[0] == 200
        (key,) = accounts.passkeys.for_user(accounts.users.list()[0].id)
        assert key.rp_id == "toilet.example.com"
        assert (
            Client(server, "toilet.example.com", PUBLIC, https=True).login(authenticator)[0] == 200
        )
        # localhost で開いたときは、外部のドメインで登録したパスキーは使えない。
        local = Client(server)
        assert local.login(authenticator, rp_id="toilet.example.com")[0] == 400


def test_ip_address_access_is_moved_to_localhost_for_passkeys(tmp_path):
    with running(tmp_path) as (server, _):
        port = server.server_port
        for path in ("/login", "/enroll?code=x"):
            status, headers, _ = exchange(server, "GET", path, auth=False)
            assert status == 303
            assert headers["Location"] == f"http://localhost:{port}{path}"


def test_pages_send_browsers_without_a_session_to_login(tmp_path):
    with running(tmp_path) as (server, _):
        browser = Client(server)
        status, headers, _ = browser.get("/records?days=7", {"Accept": "text/html"})
        assert status == 303
        location = urlsplit(headers["Location"])
        assert (location.path, location.query) == ("/login", "next=%2Frecords%3Fdays%3D7")
        status, _, page = browser.get("/login")
        assert status == 200
        assert 'data-passkey="login"' in page.decode()
        assert 'src="/assets/passkey.js"' in page.decode()
        assert browser.get("/assets/passkey.js")[0] == 200
        # 画像などページ以外は、これまでどおり 403 を返す。
        assert browser.get("/live/frame", {"Accept": "image/*"})[0] == 403


def test_invalid_json_is_rejected(tmp_path):
    with running(tmp_path) as (server, _):
        browser = Client(server)
        conn = http.client.HTTPConnection(*server.server_address)
        conn.request(
            "POST",
            "/webauthn/login/verify",
            "not json",
            browser.headers({"Content-Type": "application/json", "Origin": browser.origin}),
        )
        assert conn.getresponse().status == 400
        conn.close()
        assert browser.post("/webauthn/login/verify", {"ceremony": "x"})[0] == 400
        assert browser.post("/webauthn/unknown", {})[0] == 404


@pytest.mark.parametrize(
    "value",
    [
        "//evil.example/",
        "/\\evil.example",
        "/\t/evil.example",  # ブラウザーはタブと改行を取り除き、//evil.example として扱う
        "/\n/evil.example",
        "/\r/evil.example",
        "/\x00/evil",
        "/\x7f/evil",
        "https://evil.example/",
        "evil.example",
        "",
        None,
        42,
    ],
)
def test_safe_next_rejects_anything_that_could_leave_the_gui(value):
    from poop_detector.gui.auth_api import safe_next

    assert safe_next(value) == "/"


@pytest.mark.parametrize("value", ["/", "/records?days=7", "/clips?id=a%2Fb", "/%2F%2Fevil"])
def test_safe_next_keeps_paths_inside_the_gui(value):
    from poop_detector.gui.auth_api import safe_next

    assert safe_next(value) == value
