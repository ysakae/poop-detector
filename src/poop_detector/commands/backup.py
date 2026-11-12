"""backup・restore サブコマンド。データの保存先のバックアップを作り、別のマシンや再インストール後に戻す。

復元はデータの保存先を入れ替えるので、GUI からは行わず、監視するマシンでだけ実行する。
中身を確かめて内容を示し、確認してから入れ替える。元のデータは名前を変えて残す。
操作は、操作者を "cli" として監査ログに残す（ファイルの場所は残さない）。
"""

import argparse
from pathlib import Path

from ..gui.accounts import Accounts
from ..gui.audit import CLI_ACTOR
from ..records.backup import create_backup, inspect_backup, restore_backup
from ..records.database import events_database
from ..timezones import to_local
from .context import CommandContext, CommandError


def _audit(root: Path, action: str, detail: str) -> None:
    Accounts(events_database(root)).audit.record(CLI_ACTOR, action, detail)


def backup(args: argparse.Namespace, context: CommandContext) -> int:
    output = Path(args.output)
    try:
        file = output.open("xb")
    except FileExistsError:
        raise CommandError(
            "書き出し先のファイルが既にあります。別の名前を指定してください"
        ) from None
    try:
        with file:
            summary = create_backup(context.root, file, include_clips=args.clips)
    except BaseException:
        # "xb" で新しく作った書き出し先だけを消す（既存のファイルは開く時点で断っている）。
        output.unlink(missing_ok=True)
        raise
    _audit(context.root, "backup.create", f"{summary.files}件のファイル（動画{summary.clips}本）")
    print(f"バックアップを作りました: {output}（{summary.files}件、動画{summary.clips}本）")
    print("家の中の画像を含むので、ファイルの置き場所に気を付けてください。")
    return 0


def restore(args: argparse.Namespace, context: CommandContext) -> int:
    archive = Path(args.archive)
    if not archive.is_file():
        raise CommandError("バックアップのファイルが見つかりません")
    manifest = inspect_backup(archive)
    created = (
        to_local(manifest.created_at).strftime("%Y/%m/%d %H:%M") if manifest.created_at else "不明"
    )
    print(f"バックアップの作成: {created}")
    print(f"ファイル: {len(manifest.files)}件（動画{manifest.clips}本）")
    print(f"復元先: {context.root}")
    print("監視と GUI を止めてから続けてください。いまのデータは消さず、名前を変えて残します。")
    if not args.yes and input("復元しますか？ [y/N] ").strip().lower() not in ("y", "yes"):
        print("復元を取りやめました。")
        return 1
    preserved = restore_backup(archive, context.root)
    _audit(context.root, "backup.restore", f"{created}に作ったバックアップ")
    print("復元しました。ログインし直し、通知を受け取る端末は購読をやり直してください。")
    if preserved is not None:
        print(f"元のデータ: {preserved}")
    return 0
