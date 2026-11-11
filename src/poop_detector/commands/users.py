"""GUI のユーザーの管理（監視するマシンで実行する）。

このマシンで実行できること自体を信頼の根拠にして、最初の管理者の登録と、
パスキーをなくしたときの復旧の URL を出す。URL は使い捨てで、短い時間だけ有効。
"""

import argparse
import json

from ..gui.accounts import Accounts, User
from ..gui.invitation_mail import invite_by_email
from ..records.database import events_database
from ..web_address import Site, WebOptions
from .context import CommandContext, CommandError

CREATED_BY = "cli"


def enrollment_url(context: CommandContext, code: str) -> str:
    """登録用 URL。外部の URL があればそれを、なければ同じマシンの localhost を使う。

    パスキーは開いたドメインに結び付くので、IP アドレス（127.0.0.1）では登録できない。
    """
    web = WebOptions.from_config(context.config)
    base = web.public_url.rstrip("/") if web.public_url else f"http://localhost:{web.port}"
    return f"{base}/enroll?code={code}"


def _find(accounts: Accounts, identity: str | None) -> User:
    if not identity:
        raise CommandError("メールアドレスを指定してください")
    user = (
        accounts.users.by_email(identity) if "@" in identity else accounts.users.by_name(identity)
    )
    if user is None or ("@" not in identity and user.email is not None):
        raise CommandError("ユーザーが見つかりません。users list で確認してください")
    return user


def _show_url(context: CommandContext, code: str, minutes: float, heading: str) -> None:
    print(heading)
    print(f"  {enrollment_url(context, code)}")
    print(
        f"{minutes:g}分以内に、登録する人の端末のブラウザーで開いてください。"
        "この URL は一度だけ使えるパスワードと同じなので、本人以外に見せないでください。"
    )


def users(args: argparse.Namespace, context: CommandContext) -> int:
    accounts = Accounts(events_database(context.root))
    action = args.action
    if action == "list":
        counts = accounts.passkeys.counts()
        listed = [
            {
                "name": user.name,
                "email": user.email,
                "role": user.role,
                "active": user.active,
                "passkeys": counts.get(user.id, 0),
                "created_at": user.created_at,
                "last_login": user.last_login,
            }
            for user in accounts.users.list()
        ]
        print(json.dumps(listed, ensure_ascii=False, indent=2))
        return 0
    if action == "cancel-invite":
        if not args.identity or not accounts.enrollments.cancel_email(args.identity):
            raise CommandError("取り消せる招待が見つかりません")
        print("招待を取り消しました")
        return 0
    if action in ("invite", "resend"):
        if not args.identity:
            raise CommandError("招待する人のメールアドレスを指定してください")
        if action == "resend" or "@" in args.identity or args.name is not None:
            web = WebOptions.from_config(context.config)
            invite_by_email(
                accounts.enrollments,
                Site(web.port, web.public_url).public_base,
                args.identity,
                actor=CREATED_BY,
                name=args.name or "",
                role=args.role,
                minutes=args.minutes,
                resend=action == "resend",
            )
            print("招待メールを送信しました")
            return 0
        if args.role != "admin":
            raise CommandError(
                "一般ユーザーの招待は権限制御（#85）の実装後に使えます。初回は --role admin を指定してください"
            )
        if accounts.users.list():
            raise CommandError("招待する人のメールアドレスを指定してください")
        code = accounts.enrollments.issue(
            "admin", name=args.identity, created_by=CREATED_BY, minutes=args.minutes
        )
        _show_url(context, code, args.minutes, "初回管理者の登録用 URL:")
        return 0
    user = _find(accounts, args.identity)
    if action == "recover":
        code = accounts.enrollments.issue_recovery(
            user.id, created_by=CREATED_BY, minutes=args.minutes
        )
        _show_url(context, code, args.minutes, f"「{user.name}」にパスキーを追加する URL:")
    elif action == "disable":
        accounts.users.disable(user.id)
        print(f"「{user.name}」を無効にしました")
    else:
        accounts.users.enable(user.id)
        print(f"「{user.name}」を有効にしました")
    return 0
