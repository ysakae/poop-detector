"""通知先を置換できるインターフェース。"""

import json
import logging
import re
import subprocess
from typing import Protocol
from urllib.request import Request, urlopen

from .events import EXCRETION_TYPES

TITLE = "ここちゃんのトイレ"
logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self) -> bool: ...


class _ToiletNotifier:
    def __init__(self, enabled):
        self.enabled = enabled

    def send_test(self):
        return self._display("ここちゃんのトイレの通知テストです。")

    def send(self):
        return self._display(
            "ここちゃんのトイレに変化を検出しました。排便・排尿の可能性があります。"
        )

    def send_early_alert(self):
        return self._display(
            "トイレで同じ場所にとどまっています。ここちゃんの様子を確認してください。"
        )

    def send_assessment(self, label, *, location=None):
        if label not in ("feces", "urine", "both"):
            return False
        place = "トレー外での" if location == "outside" else ""
        return self._display(
            f"Codexが{place}{EXCRETION_TYPES[label]}の候補を検出しました。動画を確認できます。"
        )

    def _display(self, message):
        return self.enabled and self._deliver(message)


class MacNotifier(_ToiletNotifier):
    def __init__(self, enabled=True, runner=subprocess.run):
        super().__init__(enabled)
        self.runner = runner

    def _deliver(self, message):
        try:
            result = self.runner(
                [
                    "osascript",
                    "-e",
                    f'display notification "{message}" with title "{TITLE}"',
                ],
                shell=False,
                check=False,
                capture_output=True,
                timeout=5,
            )
            return result is None or result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            logger.warning("通知を送信できませんでした")
            return False


class NtfyNotifier(_ToiletNotifier):
    def __init__(self, topic, enabled=True, opener=None):
        super().__init__(enabled)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
            raise ValueError(
                "POOP_NTFY_TOPICには半角英数字・ハイフン・下線のトピック名を指定してください（64文字まで）"
            )
        self.topic = topic
        self.opener = urlopen if opener is None else opener

    def _deliver(self, message):
        payload = json.dumps(
            {"topic": self.topic, "title": TITLE, "message": message}, ensure_ascii=False
        ).encode("utf-8")
        request = Request(
            "https://ntfy.sh",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=5) as response:
                return 200 <= response.status < 300
        except OSError:
            logger.warning("ntfy.shに通知を送信できませんでした")
            return False
