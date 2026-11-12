"""外部の URL とリバースプロキシの背後で GUI を動かすときの、Host・Origin・HTTPS の扱いを検証する。"""

import threading
from contextlib import contextmanager
from urllib.parse import urlencode

import pytest
from passkey_client import Client
from soft_authenticator import SoftAuthenticator
from support import csrf_token, exchange

from poop_detector.gui.server import announce, make_server
from poop_detector.web_address import WebOptions

PUBLIC = "https://toilet.example.com"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@contextmanager
def running(tmp_path, **options):
    web = WebOptions(**{"public_url": PUBLIC, **options})
    server = make_server(tmp_path, 0, web=web)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_public_host_is_accepted_but_others_are_not(tmp_path):
    with running(tmp_path) as server:
        assert exchange(server, "GET", "/", headers={"Host": "toilet.example.com"})[0] == 200
        assert exchange(server, "GET", "/", headers={"Host": "TOILET.example.com:443"})[0] == 200
        assert exchange(server, "GET", "/", headers={"Host": "evil.example"})[0] == 403


def test_post_from_another_origin_is_rejected(tmp_path):
    with running(tmp_path) as server:
        body = urlencode({"csrf": csrf_token(server), "action": "missed", "start": "x"})
        evil = exchange(server, "POST", "/save", body, {**FORM, "Origin": "https://evil.example"})
        assert evil[0] == 403
        null = exchange(server, "POST", "/save", body, {**FORM, "Origin": "null"})
        assert null[0] == 403
        # 正しい Origin なら、CSRF の確認を経てルートが入力の誤りを返す。
        assert exchange(server, "POST", "/save", body, {**FORM, "Origin": PUBLIC})[0] == 400
        local = f"http://127.0.0.1:{server.server_port}"
        assert exchange(server, "POST", "/save", body, {**FORM, "Origin": local})[0] == 400


def test_https_from_a_trusted_proxy_adds_hsts_and_secure_cookie(tmp_path):
    with running(tmp_path) as server:
        proxied = {"Host": "toilet.example.com", "X-Forwarded-Proto": "https"}
        status, headers, _ = exchange(server, "GET", "/", headers=proxied)
        assert status == 200
        assert headers["Strict-Transport-Security"] == "max-age=31536000"
        browser = Client(server, "toilet.example.com", PUBLIC, https=True)
        code = server.app.accounts.enrollments.issue("admin", name="家族A", created_by="cli")
        assert browser.register(SoftAuthenticator(), code)[0] == 200
        assert "; Secure" in browser.last_headers["Set-Cookie"]


def test_plain_local_access_gets_no_hsts_or_secure_cookie(tmp_path):
    with running(tmp_path) as server:
        _, headers, _ = exchange(server, "GET", "/")
        assert "Strict-Transport-Security" not in headers
        browser = Client(server)
        code = server.app.accounts.enrollments.issue("admin", name="家族A", created_by="cli")
        assert browser.register(SoftAuthenticator(), code)[0] == 200
        assert "Secure" not in browser.last_headers["Set-Cookie"]


def test_forwarded_https_from_an_untrusted_client_is_ignored(tmp_path):
    with running(tmp_path, trusted_proxies="10.0.0.2") as server:
        headers = {"Host": "toilet.example.com", "X-Forwarded-Proto": "https"}
        _, response, _ = exchange(server, "GET", "/", headers=headers)
        assert "Strict-Transport-Security" not in response


def test_launch_message_shows_local_and_public_urls(capsys):
    announce("http://localhost:8765/", opened=False, public_url=PUBLIC + "/", has_users=True)
    out = capsys.readouterr().out
    assert "http://localhost:8765/" in out
    assert PUBLIC + "/" in out
    assert "users invite" not in out


def test_launch_message_explains_how_to_register_the_first_admin(capsys):
    announce("http://localhost:8765/", opened=True, public_url=None, has_users=False)
    out = capsys.readouterr().out
    assert "users invite" in out
    assert "--role admin" in out


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_server_listens_on_the_configured_address(tmp_path, host):
    server = make_server(tmp_path, 0, web=WebOptions(host=host))
    try:
        assert server.server_address[0] == host
    finally:
        server.server_close()


def test_launch_message_marks_the_public_url_as_opened(capsys):
    announce("http://localhost:8765/", opened=True, public_url=PUBLIC + "/", has_users=True)
    out = capsys.readouterr().out
    assert "GUI: http://localhost:8765/\n" in out
    assert f"外から開くとき: {PUBLIC}/（ブラウザーで開きました）" in out
