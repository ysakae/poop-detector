"""通知先を置換できるインターフェース。"""

import logging
import subprocess
from typing import Protocol


class Notifier(Protocol):
    def send(self) -> bool: ...


class MacNotifier:
    def __init__(self, enabled=True, runner=subprocess.run):
        self.enabled = enabled
        self.runner = runner

    def send(self):
        if not self.enabled:
            return False
        try:
            result = self.runner(
                [
                    "osascript",
                    "-e",
                    'display notification "ここちゃんのトイレに変化を検出しました。'
                    '排便・排尿の可能性があります。" with title "ここちゃんのトイレ"',
                ],
                shell=False,
                check=False,
                capture_output=True,
                timeout=5,
            )
            return result is None or result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            logging.getLogger(__name__).warning("通知を送信できませんでした")
            return False
