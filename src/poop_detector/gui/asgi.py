"""GUI の ASGI アプリ。安全対策をすべての要求に1か所で適用し、ルートへ振り分ける。

すべての要求は外側から順に次を通る。
1. SecurityHeaders：どの応答にも CSP などの安全のためのヘッダーを付ける。HTTPS で届いた
   要求には HSTS も付ける
2. Throttle：接続元ごとに要求の回数を数え、上限を超えたら 429 を返す（rate_limit.py）
3. Admission：Host ヘッダーが自分の待ち受け先か外部の URL か（DNS リバインディング対策）、
   POST の Origin が許した相手か（CSRF の多重の対策）を確かめ、Cookie のセッションが、
   パスキーでログインした有効なユーザーのものかを確かめる。ログインしていない相手は、
   ログイン・パスキーの登録の画面（PUBLIC_PATHS）だけを開ける

ルートへ振り分ける dispatch は、ルートの表で管理者だけと宣言したルート（routes.required_role）
を、管理者でない人には 403 で断る。

HTTPS かどうか（scope の scheme）は、信頼するプロキシからの X-Forwarded-Proto を uvicorn が
反映した値を使う。
POST は加えて、本文の大きさと CSRF トークンを確かめてからルートに渡す。
"""

import hmac
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..web_address import Site

# 次のモジュールは、読み込むとそれぞれの画面のルートを登録する（名前は使わない）。
from . import (
    account_routes,  # noqa: F401
    areas_routes,  # noqa: F401
    checks_routes,  # noqa: F401
    export_routes,  # noqa: F401
    models_routes,  # noqa: F401
    push_routes,  # noqa: F401
    review_routes,  # noqa: F401
    settings_routes,  # noqa: F401
)
from .access import (
    session_ids,
)
from .auth_api import webauthn
from .exchange import HTML, Exchange
from .navigation import Viewer
from .pages import save_failed_page
from .rate_limit import RateLimiter, RateLimits
from .review_queue import review_items
from .routes import GET_ROUTES, POST_ROUTES, Form, admin_only_page, required_role
from .templating import DISPLAY, VIEWER
from .themes import resolve

if TYPE_CHECKING:
    from .server import GuiApp

logger = logging.getLogger(__name__)
TOO_MANY = "要求が多すぎます。しばらく待ってから開き直してください。"
ACCESS_DENIED = "アクセスできません。ブラウザーで開き、パスキーでログインしてください。"
MAX_FORM_BYTES = 16384
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self' blob:; media-src 'self'; "
        "script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "manifest-src 'self'; worker-src 'self'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    # no-referrer にすると、ブラウザーは同じサイトのフォームの送信でも Origin: null を送り、
    # Admission の Origin の検査で断られてしまう。same-origin なら、ほかのサイトには URL を渡さない。
    "Referrer-Policy": "same-origin",
}
# HTTPS で届いた要求にだけ付ける。サブドメインには広げない（利用者の他のサイトに影響するため）。
HSTS = "max-age=31536000"


def request_path(scope: Scope) -> str:
    """要求の行のままのパスと問い合わせ文字列（パーセント符号化を解かない）。"""
    raw = scope.get("raw_path") or scope["path"].encode()
    path = raw.decode("latin-1")
    query = scope.get("query_string", b"").decode("latin-1")
    return f"{path}?{query}" if query else path


class SecurityHeaders:
    """すべての応答に安全のためのヘッダーを付ける。ルートが同じ名前を決めていればそれを使う。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
                if scope.get("scheme") == "https":
                    headers["Strict-Transport-Security"] = HSTS
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ログインしていなくても開ける場所（ログイン、パスキーの登録、その画面のスクリプト）。
# 肉球の模様（ワンちゃんのテーマ）は、ログインの画面の見出しにも出すので公開する。
# PWA のマニフェスト・アイコン・Service Worker は、ブラウザーが Cookie なしで読むことがあるので公開する。
PUBLIC_PATHS = frozenset(
    {
        "/login",
        "/enroll",
        "/assets/passkey.js",
        "/assets/paw.svg",
        "/manifest.webmanifest",
        "/sw.js",
        "/assets/icon-192.png",
        "/assets/icon-512.png",
    }
)
PUBLIC_PREFIX = "/webauthn/"


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIX)


def is_auth_attempt(scope: Scope) -> bool:
    """回数を厳しく数える、認証の試み（登録用 URL、パスキーの窓口）。"""
    path: str = scope["path"]
    if scope["method"] == "POST":
        return path.startswith(PUBLIC_PREFIX)
    return path == "/enroll"


def wants_page(headers: Headers, method: str) -> bool:
    """ブラウザーが画面を開こうとしている要求か（ログイン画面へ移す対象）。"""
    return method == "GET" and "text/html" in headers.get("accept", "")


def client_address(scope: Scope) -> str:
    """接続元の IP アドレス。信頼するプロキシの背後では、uvicorn が X-Forwarded-For を反映する。"""
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


class Throttle:
    """接続元ごとに、すべての要求・POST・起動トークン付きの URL の回数を制限する。"""

    def __init__(self, app: ASGIApp, limits: RateLimits) -> None:
        self.app = app
        self.limits = limits
        self.limiter = RateLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        wait = self.wait_seconds(scope)
        if wait:
            response = Response(TOO_MANY, 429, {"Retry-After": str(wait)}, media_type=HTML)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def wait_seconds(self, scope: Scope) -> int:
        client = client_address(scope)
        kinds = [("all", self.limits.all)]
        if scope["method"] == "POST":
            kinds.append(("post", self.limits.post))
        if is_auth_attempt(scope):
            kinds.append(("auth", self.limits.token))
        for kind, rule in kinds:
            wait = self.limiter.hit(client, kind, rule)
            if wait:
                logger.warning(
                    "GUI への要求が多すぎるため断りました（接続元 %s、%s）", client, kind
                )
                return wait
        return 0


class Admission:
    """Host・起動時のトークン・閲覧用の Cookie を確かめ、通さない要求にはここで応答する。"""

    def __init__(self, app: ASGIApp, gui: "GuiApp", site: Site) -> None:
        self.app = app
        self.gui = gui
        self.site = site

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response = await self.refusal(scope)
        if response is None:
            await self.app(scope, receive, send)
        else:
            await response(scope, receive, send)

    async def refusal(self, scope: Scope) -> Response | None:
        """通さないときの応答。通すなら None。"""
        headers = Headers(scope=scope)
        if (headers.get("host") or "").lower() not in self.site.hosts:
            return Response("アクセスできません", 403, media_type=HTML)
        origin = headers.get("origin")
        if scope["method"] == "POST" and origin is not None and origin not in self.site.origins:
            return Response("アクセスできません", 403, media_type=HTML)
        url = urlsplit(request_path(scope))
        found = await run_in_threadpool(self.valid_session, headers.get("cookie"))
        if found is not None:
            # ルートへは、確かめたセッションを scope の state で渡す（CSRF トークンを導く）。
            state = scope.setdefault("state", {})
            state["session_id"], state["user_id"] = found
            return None
        if is_public(url.path):
            return None
        if wants_page(headers, scope["method"]):
            target = urlencode({"next": request_path(scope)})
            return Response(b"", 303, {"Location": f"/login?{target}"})
        return Response(ACCESS_DENIED, 403, media_type=HTML)

    def valid_session(self, cookie_header: str | None) -> tuple[str, str] | None:
        """Cookie のセッションのうち有効なものの (ID, ユーザーの ID)。

        ユーザーに結び付かないセッション（廃止した起動トークン方式のもの）と、無効にした
        ユーザーのセッションは使えない。データベースを読むのでスレッドで呼ぶ。
        """
        for value in session_ids(cookie_header):
            session = self.gui.sessions.lookup(value)
            if session is None:
                continue
            user = self.gui.accounts.users.get(session.user_id) if session.user_id else None
            if user is None or not user.active:
                self.gui.sessions.revoke(value)
                continue
            return value, user.id
        return None


async def read_form(request: Request) -> Form | None:
    """URL エンコードのフォームを読む。大きすぎるか読めなければ None。"""
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        return None
    if not 0 < length <= MAX_FORM_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > length:
            return None
    try:
        data = parse_qs(bytes(body).decode(), keep_blank_values=True)
    except (ValueError, UnicodeError):
        return None
    return Form({key: values[0] for key, values in data.items()}, data)


def not_found(method: str) -> Response:
    message = "画像または記録が見つかりません" if method == "GET" else "記録が見つかりません"
    return Response(message, 404, media_type=HTML)


def _menu_path(path: str) -> str:
    """メニューで印を付ける場所。確認待ちから開いた画面（from=review）は確認待ちとする。"""
    url = urlsplit(path)
    return "/review" if parse_qs(url.query).get("from") == ["review"] else url.path


def as_viewer(route: Callable[[Exchange, Any], None], exchange: Exchange, argument: Any) -> None:
    """ルートを呼ぶ。テンプレートが共通の枠と入口を出し分けられるよう、見ている人を渡す。"""
    user = exchange.user
    viewer = (
        Viewer(
            name=user.name,
            admin=user.role == "admin",
            path=_menu_path(exchange.path),
            monitor=exchange.app.preview.monitor_status,
            pending=lambda: len(review_items(exchange.app.store, exchange.app.clips)),
        )
        if user is not None
        else None
    )
    display = resolve(
        user.theme if user else None, user.appearance if user else None, exchange.app.default_theme
    )
    token = VIEWER.set(viewer)
    display_token = DISPLAY.set(display)
    try:
        route(exchange, argument)
    finally:
        VIEWER.reset(token)
        DISPLAY.reset(display_token)


async def dispatch(request: Request) -> Response:
    """GET_ROUTES・POST_ROUTES の関数を、要求ごとの Exchange で呼ぶ。"""
    gui: GuiApp = request.app.state.gui
    # ログイン画面など、セッションがなくても開ける場所では CSRF トークンを出さない。
    state = request.scope.get("state", {})
    session_id: str | None = state.get("session_id")
    user_id: str | None = state.get("user_id")
    user = await run_in_threadpool(gui.accounts.users.get, user_id) if user_id else None
    exchange = Exchange(
        gui,
        request_path(request.scope),
        request.headers,
        request.scope.get("scheme", "http"),
        csrf=gui.csrf_for(session_id) if session_id else "",
        user=user,
    )
    url = urlsplit(exchange.path)
    if request.method not in ("GET", "POST"):
        return not_found(request.method)
    routes: Mapping[str, Callable[..., None]] = (
        GET_ROUTES if request.method == "GET" else POST_ROUTES
    )
    if url.path not in routes:
        return not_found(request.method)
    if required_role(request.method, url.path) == "admin" and (
        user is None or user.role != "admin"
    ):
        # 画面に入口を出さないだけに頼らず、ここで必ず断る。
        return Response(admin_only_page(gui.title), 403, media_type=HTML)
    if request.method == "GET":
        await run_in_threadpool(as_viewer, GET_ROUTES[url.path], exchange, parse_qs(url.query))
    else:
        form = await read_form(request)
        if form is None:
            return Response(save_failed_page(gui.title), 400, media_type=HTML)
        if not exchange.csrf or not hmac.compare_digest(form.fields.get("csrf", ""), exchange.csrf):
            return Response("画面を再読み込みして保存してください", 403, media_type=HTML)
        await run_in_threadpool(as_viewer, POST_ROUTES[url.path], exchange, form)
    if exchange.response is None:
        # ルートが応答を決めなかったのはプログラムの誤り。中身を出さずに失敗を返す。
        return Response("処理できませんでした", 500, media_type=HTML)
    return exchange.response


async def unknown_path(request: Request, _error: Exception) -> Response:
    return not_found(request.method)


def create_app(gui: "GuiApp", site: Site, limits: RateLimits) -> Starlette:
    """ルートの表から ASGI アプリを作る。表に後から足したルートも dispatch が見つける。"""
    app = Starlette(
        routes=[
            Route("/webauthn/{step:path}", webauthn, methods=["POST"]),
            Route("/{path:path}", dispatch, methods=["GET", "POST"]),
        ],
        middleware=[
            Middleware(SecurityHeaders),
            Middleware(Throttle, limits=limits),
            Middleware(Admission, gui=gui, site=site),
        ],
        exception_handlers={404: unknown_path, 405: unknown_path},
    )
    app.state.gui = gui
    return app
