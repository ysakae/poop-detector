"""通知先を置換できるインターフェース。"""

import logging
import subprocess
from typing import Protocol

from .events import EXCRETION_TYPES


class Notifier(Protocol):
    def send(self) -> bool: ...


class MacNotifier:
    def __init__(self, enabled=True, runner=subprocess.run):
        self.enabled = enabled
        self.runner = runner

    def send(self):
        return self._display(
            "ここちゃんのトイレに変化を検出しました。排便・排尿の可能性があります。"
        )

    def send_early_alert(self):
        return self._display(
            "トイレで同じ場所にとどまっています。ここちゃんの様子を確認してください。"
        )

    def send_assessment(self, label):
        if label not in ("feces", "urine", "both"):
            return False
        return self._display(
            f"Codexが{EXCRETION_TYPES[label]}の候補を検出しました。動画を確認できます。"
        )

    def _display(self, message):
        if not self.enabled:
            return False
        try:
            result = self.runner(
                [
                    "osascript",
                    "-e",
                    f'display notification "{message}" with title "ここちゃんのトイレ"',
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
