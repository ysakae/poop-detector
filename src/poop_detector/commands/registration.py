"""CLI のサブコマンドと実行関数を一緒に登録する。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

from .context import Handler


@dataclass
class CommandGroup:
    """引数パーサーに実行関数を持つサブコマンドを追加する。"""

    subparsers: argparse._SubParsersAction[argparse.ArgumentParser]

    def add(
        self, name: str, handler: Handler, *, help: str, aliases: Sequence[str] = ()
    ) -> argparse.ArgumentParser:
        parser = self.subparsers.add_parser(name, help=help, aliases=aliases)
        parser.set_defaults(handler=handler)
        return parser
