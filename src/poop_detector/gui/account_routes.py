"""自分のアカウント（パスキーの一覧・追加・削除）と、管理者による招待の画面。

ルートの表への登録はこのモジュールを読み込んだときに行う（asgi.py が読み込む）。
"""

from typing import TYPE_CHECKING

from ..errors import InvalidInputError
from .accounts import ENROLLMENT_MINUTES, ROLES, User
from .invitation_mail import MailError, invite_by_email, recover_by_email, verify_address
from .pages import Query, local_time, message_page
from .routes import Form, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

# 自分の端末にパスキーを足すときの登録用 URL は、その場で使うので短くする。
ADD_PASSKEY_MINUTES = 10


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
    _account_page(handler, query)


def _account_page(
    handler: "Handler", query: Query, *, status: int = 200, email_error: str | None = None
) -> None:
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
        email=user.email,
        email_sent=first(query, "email_sent") == "1",
        email_verified=first(query, "email_verified") == "1",
        email_error=email_error,
        role=ROLES[user.role],
        keys=keys,
        admin=user.role == "admin",
        deleted=first(query, "deleted") == "1",
    )
    handler.respond(status, page)


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


@post("/account/email")
def request_email_confirmation(handler: "Handler", form: Form) -> None:
    app = handler.app
    try:
        verify_address(
            app.accounts.enrollments,
            app.site.public_base,
            _signed_in(handler),
            form.fields.get("email", ""),
        )
    except InvalidInputError as error:
        _account_page(handler, {}, status=400, email_error=str(error))
        return
    except MailError as error:
        _account_page(handler, {}, status=502, email_error=str(error))
        return
    handler.redirect("/account?email_sent=1")


@get("/account/email/confirm")
def email_confirmation_page(handler: "Handler", query: Query) -> None:
    handler.respond(
        200,
        render_template(
            "email_confirm.html",
            title=handler.app.title,
            token=handler.csrf,
            code=first(query, "code"),
        ),
    )


@post("/account/email/confirm")
def confirm_email(handler: "Handler", form: Form) -> None:
    user = _signed_in(handler)
    if handler.app.accounts.enrollments.verify_email(form.fields.get("code", "").strip(), user.id):
        handler.redirect("/account?email_verified=1")
        return
    _account_page(
        handler,
        {},
        status=400,
        email_error="確認コードの期限とログイン中のアカウントを確認してください",
    )


def _invite_page(handler: "Handler", status: int = 200, **context: object) -> None:
    values = {"minutes": ENROLLMENT_MINUTES, "notice": None, "error": None, **context}
    handler.respond(
        status,
        render_template("invite.html", title=handler.app.title, token=handler.csrf, **values),
    )


@get("/account/invite")
def invite_page(handler: "Handler", _query: Query) -> None:
    if _require_admin(handler) is not None:
        _invite_page(handler)


@post("/account/invite")
def invite(handler: "Handler", form: Form) -> None:
    admin = _require_admin(handler)
    if admin is None:
        return
    app = handler.app
    email = form.fields.get("email", "")
    action = form.fields.get("action", "invite")
    try:
        if action == "cancel":
            if not app.accounts.enrollments.cancel_email(email):
                raise InvalidInputError("取り消せる招待が見つかりません")
            _invite_page(handler, notice="招待を取り消しました")
            return
        if action not in ("invite", "resend"):
            raise InvalidInputError("招待の操作を確認してください")
        invite_by_email(
            app.accounts.enrollments,
            app.site.public_base,
            email,
            actor=admin.id,
            name=form.fields.get("name", ""),
            role=form.fields.get("role", "admin"),
            resend=action == "resend",
        )
    except InvalidInputError as error:
        _invite_page(handler, 400, error=str(error))
    except MailError as error:
        _invite_page(handler, 502, error=str(error), retry_email=email)
    else:
        _invite_page(handler, notice="招待メールを送信しました")


def _users_page(
    handler: "Handler",
    email: str = "",
    *,
    status: int = 200,
    notice: str | None = None,
    error: str | None = None,
) -> None:
    users = handler.app.accounts.users
    try:
        found = users.by_email(email) if email else None
    except InvalidInputError as invalid:
        status, error, found = 400, str(invalid), None
    entries = ([found] if found else []) if email else users.list()
    handler.respond(
        status,
        render_template(
            "users.html",
            title=handler.app.title,
            token=handler.csrf,
            users=entries,
            roles=ROLES,
            email=email,
            notice=notice,
            error=error,
        ),
    )


@get("/account/users")
def users_page(handler: "Handler", query: Query) -> None:
    if _require_admin(handler) is not None:
        _users_page(handler, first(query, "email").strip())


@post("/account/users")
def manage_user(handler: "Handler", form: Form) -> None:
    admin = _require_admin(handler)
    if admin is None:
        return
    app = handler.app
    email = form.fields.get("email", "")
    try:
        user = app.accounts.users.by_email(email)
        if user is None:
            raise InvalidInputError("ユーザーが見つかりません")
        action = form.fields.get("action", "")
        if action == "recover":
            recover_by_email(app.accounts.enrollments, app.site.public_base, user, actor=admin.id)
            notice = "パスキー復旧メールを送信しました"
        elif action == "disable":
            app.accounts.users.disable(user.id)
            notice = "ユーザーを無効にしました"
        elif action == "enable":
            app.accounts.users.enable(user.id)
            notice = "ユーザーを有効にしました"
        else:
            raise InvalidInputError("ユーザー管理の操作を確認してください")
    except InvalidInputError as error:
        _users_page(handler, email, status=400, error=str(error))
    except MailError as error:
        _users_page(handler, email, status=502, error=str(error))
    else:
        if action == "disable" and user.id == admin.id:
            handler.redirect("/")
            return
        _users_page(handler, email, notice=notice)
