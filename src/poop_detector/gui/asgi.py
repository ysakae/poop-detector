"""GUI の ASGI アプリ。安全対策をすべての要求に1か所で適用し、ルートへ振り分ける。

すべての要求は外側から順に次を通る。
1. SecurityHeaders：どの応答にも CSP などの安全のためのヘッダーを付ける。HTTPS で届いた
   要求には HSTS も付ける
2. Throttle：接続元ごとに要求の回数を数え、上限を超えたら 429 を返す（rate_limit.py）
3. Admission：Host ヘッダーが自分の待ち受け先か外部の URL か（DNS リバインディング対策）、
   POST の Origin が許した相手か（CSRF の多重の対策）を確かめ、起動時のトークン付き URL なら
   サーバー側のセッションを始めて、その ID を Cookie に入れ、URL からトークンを取り除く
   （GET のみ）。それ以外は Cookie のセッションが有効かを確かめる

HTTPS かどうか（scope の scheme）は、信頼するプロキシからの X-Forwarded-Proto を uvicorn が
反映した値を使う。
POST は加えて、本文の大きさと CSRF トークンを確かめてからルートに渡す。
"""

import hmac
import logging
from typing import TYPE_CHECKING
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
from .access import (
    redirect_without_token,
    session_cookie,
    session_ids,
    token_in_query,
    token_matches,
)
from .auth_api import webauthn
from .exchange import HTML, Exchange
from .pages import save_failed_page
from .rate_limit import RateLimiter, RateLimits
from .routes import GET_ROUTES, POST_ROUTES, Form

if TYPE_CHECKING:
    from .server import GuiApp

logger = logging.getLogger(__name__)
TOO_MANY = "要求が多すぎます。しばらく待ってから開き直してください。"
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
PUBLIC_PATHS = frozenset({"/login", "/enroll", "/assets/passkey.js"})
PUBLIC_PREFIX = "/webauthn/"


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIX)


def is_auth_attempt(scope: Scope) -> bool:
    """回数を厳しく数える、認証の試み（起動トークン、登録用 URL、パスキーの窓口）。"""
    path: str = scope["path"]
    query = scope.get("query_string", b"").decode("latin-1")
    if scope["method"] == "POST":
        return path.startswith(PUBLIC_PREFIX)
    return path == "/enroll" or token_in_query(query) is not None


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
        candidate = token_in_query(url.query) if scope["method"] == "GET" else None
        if candidate is not None:
            secure = scope.get("scheme") == "https"
            return await self.accept_token(candidate, url.path, url.query, secure, scope)
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

    def valid_session(self, cookie_header: str | None) -> tuple[str, str | None] | None:
        """Cookie のセッションのうち有効なものの (ID, ユーザーの ID)。

        無効にしたユーザーのセッションは使えない。データベースを読むのでスレッドで呼ぶ。
        """
        for value in session_ids(cookie_header):
            session = self.gui.sessions.lookup(value)
            if session is None:
                continue
            if session.user_id is not None:
                user = self.gui.accounts.users.get(session.user_id)
                if user is None or not user.active:
                    self.gui.sessions.revoke(value)
                    continue
            return value, session.user_id
        return None

    async def accept_token(
        self, candidate: str, path: str, query: str, secure: bool, scope: Scope
    ) -> Response:
        """トークン付き URL でセッションを始め、トークンを除いた URL へ移動させる。"""
        if not token_matches(candidate, self.gui.access_token):
            logger.warning(
                "GUI の起動トークンが一致しない要求を断りました（接続元 %s）", client_address(scope)
            )
            return Response(ACCESS_DENIED, 403, media_type=HTML)
        session_id = await run_in_threadpool(self.gui.sessions.create)
        max_age = self.gui.sessions.policy.max_age_seconds
        return Response(
            b"",
            303,
            {
                "Location": redirect_without_token(path, query),
                "Set-Cookie": session_cookie(session_id, secure=secure, max_age=max_age),
            },
        )


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


async def dispatch(request: Request) -> Response:
    """GET_ROUTES・POST_ROUTES の関数を、要求ごとの Exchange で呼ぶ。"""
    gui: GuiApp = request.app.state.gui
    # ログイン画面など、セッションがなくても開ける場所では CSRF トークンを出さない。
    session_id: str | None = request.scope.get("state", {}).get("session_id")
    exchange = Exchange(
        gui,
        request_path(request.scope),
        request.headers,
        request.scope.get("scheme", "http"),
        csrf=gui.csrf_for(session_id) if session_id else "",
    )
    url = urlsplit(exchange.path)
    if request.method == "GET":
        get_route = GET_ROUTES.get(url.path)
        if get_route is None:
            return not_found("GET")
        await run_in_threadpool(get_route, exchange, parse_qs(url.query))
    else:
        post_route = POST_ROUTES.get(url.path)
        if post_route is None:
            return not_found("POST")
        form = await read_form(request)
        if form is None:
            return Response(save_failed_page(gui.title), 400, media_type=HTML)
        if not exchange.csrf or not hmac.compare_digest(form.fields.get("csrf", ""), exchange.csrf):
            return Response("画面を再読み込みして保存してください", 403, media_type=HTML)
        await run_in_threadpool(post_route, exchange, form)
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
