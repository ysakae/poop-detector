"""自分のアカウント（パスキーの一覧・追加・削除）と、管理者による招待の画面。

ルートの表への登録はこのモジュールを読み込んだときに行う（asgi.py が読み込む）。
"""

from typing import TYPE_CHECKING

from ..errors import InvalidInputError
from .accounts import ROLES, User
from .pages import Query, local_time, message_page
from .routes import Form, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

# 自分の端末にパスキーを足すときの登録用 URL は、その場で使うので短くする。
ADD_PASSKEY_MINUTES = 10
INVITE_MINUTES = 30


def _signed_in(handler: "Handler") -> User:
    """ログインしているユーザー。Admission を通った要求では必ずいる。"""
    if handler.user is None:
        raise RuntimeError("ログインしていない要求がアカウントの画面に届きました")
    return handler.user


def _require_admin(handler: "Handler") -> User | None:
    """管理者でなければ 403 を返して None。役割の確認は #85 でルートの表に広げる。"""
    user = _signed_in(handler)
    if user.role != "admin":
        handler.respond(403, "この操作は管理者だけができます")
        return None
    return user


@get("/account")
def account_page(handler: "Handler", query: Query) -> None:
    app = handler.app
    user = _signed_in(handler)
    keys = [
        {
            "id": key.id,
            "label": key.label,
            "rp_id": key.rp_id,
            "created": local_time(key.created_at),
            "last_used": local_time(key.last_used) if key.last_used else "まだ使っていません",
        }
        for key in app.accounts.passkeys.for_user(user.id)
    ]
    page = render_template(
        "account.html",
        title=app.title,
        token=handler.csrf,
        person=user.name,
        role=ROLES[user.role],
        keys=keys,
        admin=user.role == "admin",
        deleted=first(query, "deleted") == "1",
    )
    handler.respond(200, page)


@post("/account/passkeys/delete")
def delete_passkey(handler: "Handler", form: Form) -> None:
    """自分のパスキーを消す。最後の1つは消せない（ログインできなくなるため）。"""
    app = handler.app
    user = _signed_in(handler)
    keys = app.accounts.passkeys.for_user(user.id)
    target = form.fields.get("id", "")
    if target not in {key.id for key in keys}:
        page = message_page(
            "パスキーが見つかりません",
            "画面を再読み込みしてください。",
            "/account",
            "アカウントへ戻る",
            title=app.title,
        )
        handler.respond(404, page)
        return
    if len(keys) <= 1:
        page = message_page(
            "最後のパスキーは削除できません",
            "削除するとログインできなくなります。先に別の端末にパスキーを追加してください。",
            "/account",
            "アカウントへ戻る",
            title=app.title,
        )
        handler.respond(400, page)
        return
    app.accounts.passkeys.delete(target)
    handler.redirect("/account?deleted=1")


@post("/account/passkeys/add")
def add_passkey(handler: "Handler", _form: Form) -> None:
    """自分にパスキーを足す。短い期限の登録用 URL を作り、登録の画面へ移す。"""
    user = _signed_in(handler)
    code = handler.app.accounts.enrollments.issue_recovery(
        user.id, created_by=user.id, minutes=ADD_PASSKEY_MINUTES
    )
    handler.redirect(f"/enroll?code={code}")


def _invite_page(handler: "Handler", **context: object) -> str:
    values = {"url": None, "person": "", "minutes": INVITE_MINUTES, "error": None, **context}
    return render_template("invite.html", title=handler.app.title, token=handler.csrf, **values)


@get("/account/invite")
def invite_page(handler: "Handler", _query: Query) -> None:
    if _require_admin(handler) is None:
        return
    handler.respond(200, _invite_page(handler))


@post("/account/invite")
def invite(handler: "Handler", form: Form) -> None:
    """管理者が登録用 URL を作る。#85 までは管理者としてだけ招待できる。"""
    admin = _require_admin(handler)
    if admin is None:
        return
    app = handler.app
    name = form.fields.get("name", "").strip()
    try:
        if not name or len(name) > 40:
            raise InvalidInputError("名前は1〜40文字で入れてください")
        if app.accounts.users.by_name(name) is not None:
            raise InvalidInputError(f"「{name}」はすでに登録されています")
        code = app.accounts.enrollments.issue(
            "admin", name=name, created_by=admin.id, minutes=INVITE_MINUTES
        )
    except InvalidInputError as error:
        handler.respond(400, _invite_page(handler, error=str(error)))
        return
    base = app.site.public_base or f"http://localhost:{app.site.port}/"
    url = f"{base}enroll?code={code}"
    handler.respond(200, _invite_page(handler, url=url, person=name))
