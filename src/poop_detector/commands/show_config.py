"""設定ファイルから読んだ、有効な設定の表示。"""

import argparse

import yaml

from .context import CommandContext


def show_config(_args: argparse.Namespace, context: CommandContext) -> int:
    """既定値を補った設定を、設定ファイルと同じ形の YAML で表示する。"""
    config = context.config
    print(f"# 有効な機能: {'、'.join(config.enabled_features()) or 'なし'}")
    print("# run のフラグ（--codex など）は、この設定に加えて機能を有効にします。")
    print(yaml.safe_dump(config.to_sections(), allow_unicode=True, sort_keys=False), end="")
    return 0
