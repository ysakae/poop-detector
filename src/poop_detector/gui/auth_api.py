"""パスキーの登録とログインの JSON の窓口（/webauthn/...）。ログインしていなくても呼べる。

ブラウザーの passkey.js が fetch で呼ぶ。セッションがない相手からの要求なので、CSRF トークンの
代わりに、Origin が開いたドメインと一致することと、本文が JSON であることを必須にする。
"""

import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .access import session_cookie
from .passkeys import PasskeyError, relying_party

if TYPE_CHECKING:
    from .server import GuiApp

logger = logging.getLogger(__name__)
MAX_JSON_BYTES = 65536
STEPS = ("register/options", "register/verify", "login/options", "login/verify")


def safe_next(value: Any) -> str:
    """ログイン後の移動先。同じ GUI の中のパスだけを受け付け、それ以外は "/"。

    ブラウザーは URL のタブや改行を取り除き、バックスラッシュをスラッシュとして扱うので、
    制御文字とバックスラッシュを含むものは、別のサイトへ出る形になりうるとして断る。
    """
    if not isinstance(value, str) or not value.startswith("/"):
        return "/"
    if any(ord(char) < 0x20 or char in "\\\x7f" for char in value):
        return "/"
    parts = urlsplit(value)
    if value.startswith("//") or parts.scheme or parts.netloc:
        return "/"
    return value


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status)


async def _read_json(request: Request) -> dict[str, Any] | None:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_JSON_BYTES:
            return None
    try:
        data = json.loads(bytes(body))
    except (ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _logged_in(gui: "GuiApp", request: Request, user_id: str) -> JSONResponse:
    """ユーザーのセッションを始め、Cookie を付けて移動先を返す。"""
    session_id = gui.sessions.create(user_id)
    secure = request.scope.get("scheme") == "https"
    max_age = gui.sessions.policy.max_age_seconds
    return JSONResponse(
        {"redirect": request.state.next_path},
        headers={"Set-Cookie": session_cookie(session_id, secure=secure, max_age=max_age)},
    )


async def webauthn(request: Request) -> Response:
    """/webauthn/{step} の振り分け。"""
    gui: GuiApp = request.app.state.gui
    step = request.path_params["step"]
    if step not in STEPS:
        return _error(404, "見つかりません")
    rp = relying_party(gui.site, request.headers.get("host"))
    if rp is None:
        return _error(400, "パスキーは IP アドレスでは使えません。localhost で開いてください")
    if request.headers.get("origin") != rp.origin:
        return _error(403, "アクセスできません")
    data = await _read_json(request)
    if data is None:
        return _error(400, "要求の形が正しくありません")
    auth = gui.passkeys
    try:
        if step == "register/options":
            ceremony, options = await run_in_threadpool(
                auth.begin_registration, str(data.get("code", "")), rp
            )
            return JSONResponse({"ceremony": ceremony, "publicKey": options})
        if step == "login/options":
            ceremony, options = await run_in_threadpool(auth.begin_login, rp)
            return JSONResponse({"ceremony": ceremony, "publicKey": options})
        credential = data.get("credential")
        if not isinstance(credential, dict):
            raise PasskeyError("要求の形が正しくありません")
        ceremony_id = str(data.get("ceremony", ""))
        if step == "register/verify":
            user_id = await run_in_threadpool(
                auth.finish_registration, ceremony_id, credential, data.get("label")
            )
            request.state.next_path = "/"
        else:
            user_id = await run_in_threadpool(auth.finish_login, ceremony_id, credential)
            request.state.next_path = safe_next(data.get("next"))
        return await run_in_threadpool(_logged_in, gui, request, user_id)
    except PasskeyError as error:
        return _error(400, str(error))
