"""ラベル GUI のすべてのルートに、同じ安全対策が適用されることを検証する。

ルートの表（GET_ROUTES・POST_ROUTES）から一覧を作るので、ルートを足すと自動で対象になる。
"""

from urllib.parse import urlencode

import pytest
from support import csrf_token, exchange

from poop_detector.gui import routes
from poop_detector.gui.asgi import PUBLIC_PATHS, SECURITY_HEADERS, is_public
from poop_detector.gui.routes import GET_ROUTES, POST_ROUTES

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


PROTECTED_GET = sorted(path for path in GET_ROUTES if not is_public(path))


def test_only_login_and_enrollment_are_public():
    """ログインしていなくても開ける場所は、ログイン・パスキーの登録・その画面のスクリプトだけ。"""
    assert {path for path in GET_ROUTES if is_public(path)} == PUBLIC_PATHS
    assert not any(is_public(path) for path in POST_ROUTES)


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_every_get_route_requires_the_access_cookie(gui, path):
    server, _, _ = gui
    status, _, body = exchange(server, "GET", path, auth=False)
    assert status == 403
    assert b"\xe3" in body  # 日本語の案内だけを返し、画面や画像は返さない
    assert b"<img" not in body
    assert b"\xff\xd8" not in body


@pytest.mark.parametrize("path", sorted(GET_ROUTES))
def test_every_get_route_rejects_other_hosts(gui, path):
    server, _, _ = gui
    status, _, _ = exchange(server, "GET", path, headers={"Host": "example.com"})
    assert status == 403


@pytest.mark.parametrize("path", sorted(POST_ROUTES))
def test_every_post_route_requires_cookie_csrf_and_host(gui, path):
    server, _, _ = gui
    body = urlencode({"csrf": csrf_token(server), "id": "x"})
    assert exchange(server, "POST", path, body, FORM, auth=False)[0] == 403
    assert exchange(server, "POST", path, body, {**FORM, "Host": "example.com"})[0] == 403
    assert exchange(server, "POST", path, urlencode({"csrf": "wrong"}), FORM)[0] == 403
    # 本文がない・大きすぎる要求は、ルートに渡さずに断る。
    assert exchange(server, "POST", path, "", FORM)[0] == 400
    assert exchange(server, "POST", path, "x" * 20000, FORM)[0] == 400


@pytest.mark.parametrize("path", ["/", "/records", "/clips", "/clips/storage", "/activity"])
def test_pages_carry_security_headers(gui, path):
    server, _, _ = gui
    status, headers, _ = exchange(server, "GET", path)
    assert status == 200
    for name, value in SECURITY_HEADERS.items():
        assert headers[name] == value


def test_redirects_carry_security_headers(gui):
    server, _, event = gui
    body = urlencode({"csrf": csrf_token(server), "id": event, "label": "urine"})
    status, headers, _ = exchange(server, "POST", "/save", body, FORM)
    assert status == 303
    assert headers["Location"] == "/?filter=unlabeled&saved=1"
    assert headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]


def test_unknown_paths_need_the_cookie_before_returning_404(gui):
    server, _, _ = gui
    assert exchange(server, "GET", "/no-such-page", auth=False)[0] == 403
    assert exchange(server, "GET", "/no-such-page")[0] == 404
    assert exchange(server, "POST", "/no-such-form", "a=1", FORM)[0] == 404


def test_new_route_is_one_function_and_is_protected(gui):
    """ルートの追加は関数1つで済み、安全対策は自動でかかる。"""

    @routes.get("/hello")
    def hello(handler, query):
        handler.respond(200, "こんにちは " + routes.first(query, "name", "犬"))

    try:
        server, _, _ = gui
        assert (
            exchange(server, "GET", "/hello?" + urlencode({"name": "ポチ"}))[2].decode()
            == "こんにちは ポチ"
        )
        assert exchange(server, "GET", "/hello", auth=False)[0] == 403
    finally:
        GET_ROUTES.pop("/hello", None)
