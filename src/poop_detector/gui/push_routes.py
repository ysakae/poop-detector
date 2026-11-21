"""PWA（マニフェスト・Service Worker・アイコン）と、Web Push の購読の登録。

マニフェストとアイコンはブラウザーが Cookie なしで読むので、ログインしていなくても開ける
（asgi.PUBLIC_PATHS）。Service Worker はサイト全体を扱えるよう、ルートの /sw.js で返す。
購読の登録・解除はログインした本人の端末だけを扱い、宛先は web_push.valid_endpoint で既知の
プッシュサービスに限る。ルートの表への登録はこのモジュールを読み込んだときに行う。
"""

import json
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..web_push import WebPushNotifier, push_subject
from .accounts import User
from .assets import serve_asset
from .pages import Query
from .routes import ASSETS, Form, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

JSON = "application/json; charset=utf-8"
# iPhone・iPad の Safari が、ページの指定を読む前に探すことのあるアイコンの場所。
APPLE_PROBES = ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png")


@get("/manifest.webmanifest")
def manifest(handler: "Handler", _query: object) -> None:
    app = handler.app
    body = {
        "name": app.title,
        "short_name": "トイレ",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        # style.css のライトの --bg。アイコンの地（--primary）と合わせて、起動画面の色にする。
        "background_color": "#faf7f2",
        "theme_color": "#faf7f2",
        "lang": "ja",
        "icons": [
            {"src": f"/assets/icon-{size}.png", "sizes": f"{size}x{size}", "type": "image/png"}
            for size in (192, 512)
        ],
    }
    handler.respond(200, json.dumps(body, ensure_ascii=False), "application/manifest+json")


@get("/sw.js")
def service_worker(handler: "Handler", _query: object) -> None:
    handler.respond(200, (ASSETS / "sw.js").read_bytes(), "text/javascript; charset=utf-8")


@get(
    "/assets/icon-192.png",
    "/assets/icon-512.png",
    "/assets/apple-touch-icon.png",
    *APPLE_PROBES,
)
def app_icon(handler: "Handler", query: Query) -> None:
    """ホーム画面のアイコン（scripts/make-app-icons.py で作る）。"""
    # マニフェストと Service Worker は版のない URL で参照するので、確かめてから使ってもらう。
    path = urlsplit(handler.path).path
    name = "apple-touch-icon.png" if path in APPLE_PROBES else path.rsplit("/", 1)[1]
    serve_asset(handler, name, "image/png", first(query, "v"))


def _user(handler: "Handler") -> User:
    if handler.user is None:
        raise RuntimeError("ログインしていない要求が通知の設定に届きました")
    return handler.user


def _result(handler: "Handler", ok: bool, error: str = "") -> None:
    body = {"ok": True} if ok else {"ok": False, "error": error}
    handler.respond(200 if ok else 400, json.dumps(body, ensure_ascii=False), JSON)


@post("/account/push/subscribe")
def subscribe(handler: "Handler", form: Form) -> None:
    fields = form.fields
    try:
        handler.app.push_subscriptions.add(
            _user(handler).id,
            fields.get("endpoint", ""),
            fields.get("p256dh", ""),
            fields.get("auth", ""),
        )
    except ValueError as error:
        _result(handler, False, str(error))
        return
    _result(handler, True)


@post("/account/push/unsubscribe")
def unsubscribe(handler: "Handler", form: Form) -> None:
    handler.app.push_subscriptions.remove(_user(handler).id, form.fields.get("endpoint", ""))
    _result(handler, True)


@post("/account/push/preferences")
def preferences(handler: "Handler", form: Form) -> None:
    user = _user(handler)
    handler.app.accounts.users.set_push(
        user.id,
        alerts=form.fields.get("alerts") == "1",
        early=form.fields.get("early") == "1",
        feeding=form.fields.get("feeding") == "1",
        # 監視の異常の通知は管理者だけに届くので、管理者だけが選べる。
        watchdog=form.fields.get("watchdog") == "1" if user.role == "admin" else None,
    )
    handler.redirect("/account?push_saved=1")


@post("/account/push/test")
def send_test_push(handler: "Handler", _form: Form) -> None:
    """自分の端末にだけテストの通知を送る（受け取り方の選択に関わらず送る）。"""
    app = handler.app
    mine = app.push_subscriptions.for_user(_user(handler).id)
    if not mine:
        message, status = "先に「この端末で通知を受け取る」を押してください。", 400
    else:
        notifier = WebPushNotifier(
            app.push_subscriptions,
            app.push_keys,
            subject=push_subject(app.site.public_base or ""),
            url="/account",
        )
        sent = notifier.send_test_to(mine)
        message, status = (
            (f"テストの通知を送りました（{sent}台）。", 200)
            if sent
            else ("通知を送れませんでした。購読をやり直してください。", 502)
        )
    page = render_template(
        "message.html",
        title=app.title,
        heading="通知のテスト",
        message=message,
        back_url="/account",
        back_text="自分の設定へ戻る",
        wrap_link=True,
    )
    handler.respond(status, page)
