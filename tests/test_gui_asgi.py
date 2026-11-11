"""GUI の ASGI の基盤（ミドルウェア、フォームの読み込み、サーバーの起動と停止）を検証する。

ルートごとの安全対策は test_gui_routes.py、画面の内容は test_label_gui.py にある。
"""

import asyncio
import logging
import threading

import pytest
from starlette.requests import Request
from support import exchange

from poop_detector.gui import routes
from poop_detector.gui.asgi import Admission, SecurityHeaders, read_form
from poop_detector.gui.routes import GET_ROUTES
from poop_detector.gui.server import make_server

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def test_enrollment_code_is_not_written_to_any_log(tmp_path, caplog):
    """登録用 URL の値はパスワードと同じなので、アクセスログなどに残さない。"""
    caplog.set_level(logging.DEBUG)
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code = server.app.accounts.enrollments.issue("admin", name="家族A", created_by="cli")
        localhost = {"Host": f"localhost:{server.server_port}"}
        status, _, _ = exchange(
            server, "GET", f"/enroll?code={code}", headers=localhost, auth=False
        )
        assert status == 200
        exchange(server, "GET", "/records")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert code not in caplog.text
    assert "/records" not in caplog.text


def test_shutdown_before_serving_does_not_block(tmp_path):
    with make_server(tmp_path, 0) as server:
        server.shutdown()


def test_unsupported_methods_get_the_same_not_found(gui):
    server, _, _ = gui
    status, headers, body = exchange(server, "PUT", "/", "a=1", FORM)
    assert status == 404
    assert "記録が見つかりません" in body.decode()
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_route_without_a_response_fails_without_details(gui):
    @routes.get("/silent")
    def silent(handler, query):
        pass

    try:
        server, _, _ = gui
        status, _, body = exchange(server, "GET", "/silent")
        assert status == 500
        assert "処理できませんでした" in body.decode()
    finally:
        GET_ROUTES.pop("/silent", None)


def test_undecodable_form_is_rejected_before_the_route(gui):
    server, _, _ = gui
    assert exchange(server, "POST", "/save", b"csrf=%ff\xff", FORM)[0] == 400


def read(chunks, length):
    """read_form を、HTTP の処理を通さずに直接呼ぶ。"""

    async def receive():
        body = chunks.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}

    scope = {"type": "http", "method": "POST", "headers": [(b"content-length", length)]}
    return asyncio.run(read_form(Request(scope, receive)))


def test_read_form_rejects_a_content_length_that_is_not_a_number():
    # HTTP 経由では uvicorn（h11）が先に断って接続を閉じるので、直接確かめる。
    assert read([b"csrf=x"], b"abc") is None


def test_read_form_stops_at_a_body_longer_than_declared():
    assert read([b"csrf=", b"x" * 100], b"10") is None


@pytest.mark.parametrize("middleware", [SecurityHeaders, Admission])
def test_middleware_passes_other_protocols_through(middleware):
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    wrapped = middleware(app) if middleware is SecurityHeaders else middleware(app, None, 0)
    asyncio.run(wrapped({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]
