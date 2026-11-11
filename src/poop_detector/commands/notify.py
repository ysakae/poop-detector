"""通知のテスト送信。"""

import argparse

from ..config import load_connection_settings
from .context import CommandContext, CommandError, build_notifier


def test_notification(args: argparse.Namespace, context: CommandContext) -> int:
    settings = load_connection_settings()
    notifier, uses_ntfy = build_notifier(context.config, settings, sends=True)
    if args.early_alert and not settings["POOP_NTFY_EARLY_TOPIC"].strip():
        raise CommandError("見守り用の POOP_NTFY_EARLY_TOPIC を設定してください")
    if not notifier.send_test(early_alert=args.early_alert):
        raise CommandError("通知が無効、非対応OS、または送信に失敗しました")
    if uses_ntfy:
        print("ntfyが通知を受け付けました。購読している端末で確認してください")
    else:
        print("通知要求を送信しました。通知センターで表示を確認してください")
    return 0
