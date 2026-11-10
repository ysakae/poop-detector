"""通知先を置換できるインターフェース。"""

import json
import logging
import re
import subprocess
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .events import EXCRETION_TYPES

DEFAULT_NTFY_SERVER = "https://ntfy.sh"
logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self) -> bool: ...


class _ToiletNotifier:
    """通知の文面を組み立てる。送信方法はサブクラスの _deliver が担う。"""

    def __init__(self, enabled, pet_name=""):
        self.enabled = enabled
        # 名前が未設定なら「◯◯の」を付けない文面にする。
        self.owner = f"{pet_name}の" if pet_name else ""

    def title(self, *, early_alert=False):
        if early_alert:
            return f"{self.owner}見守り" if self.owner else "トイレの見守り"
        return f"{self.owner}トイレ" if self.owner else "トイレの通知"

    def send_test(self, *, early_alert=False):
        kind = "見守り" if early_alert else "トイレ"
        return self._display(f"{self.owner}{kind}の通知テストです。", early_alert=early_alert)

    def send(self):
        return self._display(
            f"{self.owner}トイレに変化を検出しました。排便・排尿の可能性があります。"
        )

    def send_early_alert(self):
        return self._display(
            f"トイレで同じ場所にとどまっています。{self.owner}様子を確認してください。",
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


def applescript_string(value):
    """AppleScript の二重引用符の文字列として安全に埋め込めるようにする。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class MacNotifier(_ToiletNotifier):
    def __init__(self, enabled=True, runner=subprocess.run, *, pet_name=""):
        super().__init__(enabled, pet_name)
        self.runner = runner

    def _deliver(self, message, *, early_alert):
        script = (
            f'display notification "{applescript_string(message)}" '
            f'with title "{applescript_string(self.title())}"'
        )
        try:
            result = self.runner(
                ["osascript", "-e", script],
                shell=False,
                check=False,
                capture_output=True,
                timeout=5,
            )
            return result is None or result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            logger.warning("通知を送信できませんでした")
            return False


def validated_ntfy_server(server):
    """送信先の URL を検証し、末尾のスラッシュを除いて返す。"""
    parts = urlsplit(server.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError(
            "POOP_NTFY_SERVERには https:// または http:// で始まる ntfy サーバーの URL を指定してください"
        )
    # 認証情報や問い合わせ文字列はログや画面に漏れる恐れがあるため受け付けない。
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(
            "POOP_NTFY_SERVERには認証情報や ? 以降を含めず、サーバーの URL だけを指定してください"
        )
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


class NtfyNotifier(_ToiletNotifier):
    def __init__(
        self,
        topic,
        enabled=True,
        opener=None,
        *,
        early_topic="",
        pet_name="",
        server=DEFAULT_NTFY_SERVER,
    ):
        super().__init__(enabled, pet_name)
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
        self.server = validated_ntfy_server(server)
        self.opener = urlopen if opener is None else opener

    def _deliver(self, message, *, early_alert):
        separate = early_alert and bool(self.early_topic)
        topic = self.early_topic if separate else self.topic
        payload = json.dumps(
            {"topic": topic, "title": self.title(early_alert=separate), "message": message},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.server,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=5) as response:
                return 200 <= response.status < 300
        except OSError:
            logger.warning("ntfyに通知を送信できませんでした")
            return False
