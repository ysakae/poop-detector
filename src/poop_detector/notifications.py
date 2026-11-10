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

    def send_test(self, *, early_alert=False):
        kind = "見守り" if early_alert else "トイレ"
        return self._display(f"ここちゃんの{kind}の通知テストです。", early_alert=early_alert)

    def send(self):
        return self._display(
            "ここちゃんのトイレに変化を検出しました。排便・排尿の可能性があります。"
        )

    def send_early_alert(self):
        return self._display(
            "トイレで同じ場所にとどまっています。ここちゃんの様子を確認してください。",
            early_alert=True,
        )

    def send_assessment(self, label, *, location=None):
        if label not in ("feces", "urine", "both"):
            return False
        place = "トレー外での" if location == "outside" else ""
        return self._display(
            f"Codexが{place}{EXCRETION_TYPES[label]}の候補を検出しました。動画を確認できます。"
        )

    def _display(self, message, *, early_alert=False):
        return self.enabled and self._deliver(message, early_alert=early_alert)


class MacNotifier(_ToiletNotifier):
    def __init__(self, enabled=True, runner=subprocess.run):
        super().__init__(enabled)
        self.runner = runner

    def _deliver(self, message, *, early_alert):
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
    def __init__(self, topic, enabled=True, opener=None, *, early_topic=""):
        super().__init__(enabled)
        targets = {"POOP_NTFY_TOPIC": topic}
        if early_topic:
            targets["POOP_NTFY_EARLY_TOPIC"] = early_topic
            if early_topic == topic:
                raise ValueError(
                    "POOP_NTFY_EARLY_TOPICには通常通知と異なるトピック名を指定してください"
                )
        for name, value in targets.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                raise ValueError(
                    f"{name}には半角英数字・ハイフン・下線のトピック名を指定してください（64文字まで）"
                )
        self.topic = topic
        self.early_topic = early_topic
        self.opener = urlopen if opener is None else opener

    def _deliver(self, message, *, early_alert):
        if early_alert and self.early_topic:
            topic, title = self.early_topic, "ここちゃんの見守り"
        else:
            topic, title = self.topic, TITLE
        payload = json.dumps(
            {"topic": topic, "title": title, "message": message}, ensure_ascii=False
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
