"""GUI の HTTP サーバー。安全対策をすべての要求に1か所で適用し、ルートへ振り分ける。

すべての要求は Handler._admit を通る。
1. Host ヘッダーが自分の待ち受け先か（DNS リバインディング対策）
2. 起動時のトークン付き URL なら、トークンを Cookie に移して URL から取り除く（GET のみ）
3. 閲覧用の Cookie を持つか
POST は加えて、本文の大きさと CSRF トークンを確かめてから処理する。
応答には respond・redirect が CSP などの安全のためのヘッダーを付ける。
"""

import errno
import hmac
import secrets
import sys
import threading
import webbrowser
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..capture.live_preview import LivePreview
from ..errors import UserError
from ..records.events import Store
from ..video.clip_storage import DEFAULT_DISK_MIB
from ..video.clips import ClipStore
from .access import (
    has_session,
    launch_url,
    redirect_without_token,
    session_cookie,
    token_in_query,
    token_matches,
)
from .pages import page_title, save_failed_page
from .routes import GET_ROUTES, POST_ROUTES, Form

ACCESS_DENIED = (
    "アクセスできません。GUI を起動した端末に表示されたトークン付きの URL を開いてください。"
)
MAX_FORM_BYTES = 16384
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self' blob:; media-src 'self'; "
        "script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
}


@dataclass(frozen=True)
class GuiApp:
    """ルートが使う依存。起動ごとのトークンもここに持つ。"""

    root: Path
    store: Store
    clips: ClipStore
    preview: LivePreview
    # 保存操作の CSRF 対策に使い、画面のフォームに埋め込む。
    csrf_token: str
    # 閲覧を含むすべての要求の認証に使い、Cookie で受け取る。
    access_token: str
    title: str
    disk_mb: float


class LabelServer(ThreadingHTTPServer):
    """GUI のサーバー。ハンドラーは app からストアやトークンを受け取る。"""

    app: GuiApp

    @property
    def access_token(self) -> str:
        return self.app.access_token


class Handler(BaseHTTPRequestHandler):
    """要求ごとに安全対策を確かめ、GET_ROUTES・POST_ROUTES の関数へ振り分ける。"""

    server: LabelServer

    @property
    def app(self) -> GuiApp:
        return self.server.app

    def log_message(self, format: str, *args: Any) -> None:
        # 要求の URL には画像の ID などが入るため、標準エラーに出さない。
        pass

    def respond(
        self,
        status: int,
        data: str | bytes,
        mime: str = "text/html; charset=utf-8",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        body = data.encode() if isinstance(data, str) else data
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        for name, value in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str, headers: Mapping[str, str] | None = None) -> None:
        """303 で同じサーバーの別の画面へ移す。"""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for name, value in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(name, value)
        self.end_headers()

    def host_allowed(self) -> bool:
        """DNS リバインディングを防ぐため、Host ヘッダーが自分の待ち受け先か確かめる。"""
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def authorized(self) -> bool:
        """起動時のトークンを移した Cookie を持つ要求か。"""
        return has_session(self.headers.get("Cookie"), self.app.access_token)

    def _admit(self, *, accept_token: bool) -> bool:
        """すべての要求に共通の安全対策。通さないときは応答を返して False。"""
        if not self.host_allowed():
            self.respond(403, "アクセスできません")
            return False
        url = urlsplit(self.path)
        candidate = token_in_query(url.query) if accept_token else None
        if candidate is not None:
            self._accept_token(candidate, url.path, url.query)
            return False
        if not self.authorized():
            self.respond(403, ACCESS_DENIED)
            return False
        return True

    def _accept_token(self, candidate: str, path: str, query: str) -> None:
        """トークン付き URL を Cookie に移し、トークンを除いた URL へ移動させる。"""
        if not token_matches(candidate, self.app.access_token):
            self.respond(403, ACCESS_DENIED)
            return
        self.redirect(
            redirect_without_token(path, query),
            {"Set-Cookie": session_cookie(self.app.access_token)},
        )

    def do_GET(self) -> None:
        if not self._admit(accept_token=True):
            return
        url = urlsplit(self.path)
        route = GET_ROUTES.get(url.path)
        if route is None:
            self.respond(404, "画像または記録が見つかりません")
            return
        route(self, parse_qs(url.query))

    def do_POST(self) -> None:
        if not self._admit(accept_token=False):
            return
        route = POST_ROUTES.get(self.path)
        if route is None:
            self.respond(404, "記録が見つかりません")
            return
        form = self._read_form()
        if form is None:
            self.respond(400, save_failed_page(self.app.title))
            return
        if not hmac.compare_digest(form.fields.get("csrf", ""), self.app.csrf_token):
            self.respond(403, "画面を再読み込みして保存してください")
            return
        route(self, form)

    def _read_form(self) -> Form | None:
        """URL エンコードのフォームを読む。大きすぎるか読めなければ None。"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_FORM_BYTES:
                return None
            data = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        except (ValueError, UnicodeError):
            return None
        return Form({key: values[0] for key, values in data.items()}, data)


def make_server(
    root: str | Path,
    port: int = 8765,
    *,
    disk_mb: float = DEFAULT_DISK_MIB,
    pet_name: str = "",
) -> LabelServer:
    """127.0.0.1 だけで待ち受けるGUI のサーバーを作る。

    起動ごとに、保存操作の CSRF トークンと、閲覧を含むすべての要求に使うアクセストークンを作る。
    """
    try:
        server = LabelServer(("127.0.0.1", port), Handler)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            raise UserError(
                f"GUIのポート {port} は使用中です。gui --port または run --gui-port で変更してください"
            ) from None
        raise
    server.app = GuiApp(
        root=Path(root),
        store=Store(root),
        clips=ClipStore(root),
        preview=LivePreview(root),
        csrf_token=secrets.token_urlsafe(32),
        access_token=secrets.token_urlsafe(32),
        title=page_title(pet_name),
        disk_mb=disk_mb,
    )
    return server


def announce(base: str, url: str, *, opened: bool, interactive: bool) -> None:
    """起動の案内を出す。トークン付きの URL は端末に直接出すときだけ表示し、ログに残さない。"""
    if interactive:
        print(f"GUI: {url}", flush=True)  # noqa: T201
        print("この URL は起動中だけ有効なパスワードと同じです。他人に見せないでください。")  # noqa: T201
    elif opened:
        print(f"GUIをブラウザーで開きました: {base}", flush=True)  # noqa: T201
    else:
        print(  # noqa: T201
            "トークン付きの URL は端末にだけ表示します。出力をファイルなどへ送らずに起動するか、"
            "--no-browser を外してブラウザーで開いてください。",
            flush=True,
        )


def open_gui_url(server: LabelServer, open_browser: bool) -> None:
    base = f"http://127.0.0.1:{server.server_port}/"
    url = launch_url(base, server.access_token)
    opened = open_browser and webbrowser.open(url)
    announce(base, url, opened=opened, interactive=sys.stdout.isatty())


@contextmanager
def background_gui(
    root: str | Path,
    port: int,
    open_browser: bool,
    *,
    disk_mb: float,
    pet_name: str,
) -> Iterator[None]:
    """監視中は別スレッドで GUI を提供し、監視が終われば待ち受けを閉じる。"""
    with make_server(root, port, disk_mb=disk_mb, pet_name=pet_name) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.1}, name="poop-detector-gui"
        )
        thread.start()
        try:
            open_gui_url(server, open_browser)
            yield
        finally:
            server.shutdown()
            thread.join()


def serve(
    root: str | Path,
    port: int = 8765,
    open_browser: bool = True,
    *,
    disk_mb: float = DEFAULT_DISK_MIB,
    pet_name: str = "",
) -> None:
    with make_server(root, port, disk_mb=disk_mb, pet_name=pet_name) as server:
        open_gui_url(server, open_browser)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("GUIを終了しました")  # noqa: T201
