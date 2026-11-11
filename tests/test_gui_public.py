"""外部の URL とリバースプロキシの背後で GUI を動かすときの、Host・Origin・HTTPS の扱いを検証する。"""

import threading
from contextlib import contextmanager
from urllib.parse import urlencode

import pytest
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
        status, headers, _ = exchange(
            server, "GET", f"/?token={server.access_token}", headers=proxied, auth=False
        )
        assert status == 303
        assert "; Secure" in headers["Set-Cookie"]


def test_plain_local_access_gets_no_hsts_or_secure_cookie(tmp_path):
    with running(tmp_path) as server:
        _, headers, _ = exchange(server, "GET", "/")
        assert "Strict-Transport-Security" not in headers
        _, headers, _ = exchange(server, "GET", f"/?token={server.access_token}", auth=False)
        assert "Secure" not in headers["Set-Cookie"]


def test_forwarded_https_from_an_untrusted_client_is_ignored(tmp_path):
    with running(tmp_path, trusted_proxies="10.0.0.2") as server:
        headers = {"Host": "toilet.example.com", "X-Forwarded-Proto": "https"}
        _, response, _ = exchange(server, "GET", "/", headers=headers)
        assert "Strict-Transport-Security" not in response


def test_launch_message_shows_the_public_url_only_on_a_terminal(capsys):
    announce(
        "http://127.0.0.1:8765/",
        "http://127.0.0.1:8765/?token=T",
        opened=False,
        interactive=True,
        public_url="https://toilet.example.com/?token=T",
    )
    out = capsys.readouterr().out
    assert "https://toilet.example.com/?token=T" in out
    announce("http://127.0.0.1:8765/", "x", opened=False, interactive=False, public_url="p?token=T")
    assert "token=T" not in capsys.readouterr().out


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_server_listens_on_the_configured_address(tmp_path, host):
    server = make_server(tmp_path, 0, web=WebOptions(host=host))
    try:
        assert server.server_address[0] == host
    finally:
        server.server_close()
