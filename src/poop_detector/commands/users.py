"""GUI のユーザーの管理（監視するマシンで実行する）。

このマシンで実行できること自体を信頼の根拠にして、最初の管理者の登録と、
パスキーをなくしたときの復旧の URL を出す。URL は使い捨てで、短い時間だけ有効。
"""

import argparse
import json

from ..gui.accounts import ROLES, Accounts, User
from ..records.database import events_database
from ..web_address import WebOptions
from .context import CommandContext, CommandError

CREATED_BY = "cli"


def enrollment_url(context: CommandContext, code: str) -> str:
    """登録用 URL。外部の URL があればそれを、なければ同じマシンの localhost を使う。

    パスキーは開いたドメインに結び付くので、IP アドレス（127.0.0.1）では登録できない。
    """
    web = WebOptions.from_config(context.config)
    base = web.public_url.rstrip("/") if web.public_url else f"http://localhost:{web.port}"
    return f"{base}/enroll?code={code}"


def _find(accounts: Accounts, name: str | None) -> User:
    if not name:
        raise CommandError("ユーザーの名前を指定してください")
    user = accounts.users.by_name(name)
    if user is None:
        raise CommandError(f"「{name}」というユーザーはいません。users list で確認してください")
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
    if action == "invite":
        if not args.name:
            raise CommandError("登録する人の名前を指定してください（例: users invite 家族A）")
        if accounts.users.by_name(args.name) is not None:
            raise CommandError(
                f"「{args.name}」はすでに登録されています。復旧は users recover です"
            )
        if args.role != "admin":
            # 権限の確認（#85）が入るまでは、一般ユーザーも管理者と同じ操作ができてしまう。
            raise CommandError(
                "一般ユーザーの招待は、管理者と一般ユーザーの権限を分ける機能（#85）が入るまで"
                "使えません。いまは管理者として招待してください（--role admin）"
            )
        code = accounts.enrollments.issue(
            args.role, name=args.name, created_by=CREATED_BY, minutes=args.minutes
        )
        _show_url(context, code, args.minutes, f"{ROLES[args.role]}「{args.name}」の登録用 URL:")
        return 0
    user = _find(accounts, args.name)
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
