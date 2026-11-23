"""通知先を置換できるインターフェース。"""

import json
import logging
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any, Literal, TypeAlias
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .features.outages import format_duration
from .records.events import EXCRETION_TYPES
from .timezones import to_local

Delivery: TypeAlias = Literal["sent", "retry", "unknown", "skipped"]


def merge_delivery(results: Sequence[Delivery]) -> Delivery:
    """部分成功や応答が不明のときは、再送による重複を避ける。"""
    for status in ("sent", "unknown", "retry"):
        if status in results:
            return status
    return "skipped"


DEFAULT_NTFY_SERVER = "https://ntfy.sh"
logger = logging.getLogger(__name__)


def _clip_notice_path(clip_id: str | None) -> str:
    return "/clips?" + urlencode({"id": clip_id, "notice": "1"}) if clip_id else ""


def _notice_time(detected_at: str | None) -> str:
    if detected_at is None:
        return ""
    return to_local(detected_at).strftime("%m/%d %H:%M:%S")


class ToiletNotifier:
    """通知の文面を組み立てる。送信方法はサブクラスの _deliver が担う。"""

    def __init__(self, enabled: bool, pet_name: str = "") -> None:
        self.enabled = enabled
        # 名前が未設定なら「◯◯の」を付けない文面にする。
        self.owner = f"{pet_name}の" if pet_name else ""

    def title(self, *, early_alert: bool = False) -> str:
        if early_alert:
            return f"{self.owner}見守り" if self.owner else "トイレの見守り"
        return f"{self.owner}トイレ" if self.owner else "トイレの通知"

    def send_test(self, *, early_alert: bool = False) -> bool:
        kind = "見守り" if early_alert else "トイレ"
        return self._display(f"{self.owner}{kind}の通知テストです。", early_alert=early_alert)

    def send(self, *, clip_id: str | None = None, detected_at: str | None = None) -> bool:
        time = _notice_time(detected_at)
        return self._display(
            f"{self.owner}トイレに変化を検出しました。排便・排尿の可能性があります。"
            + (f" 検知 {time}" if time else ""),
            path=_clip_notice_path(clip_id),
        )

    def send_early_alert(
        self, *, clip_id: str | None = None, detected_at: str | None = None
    ) -> bool:
        time = _notice_time(detected_at)
        return self._display(
            f"トイレで同じ場所にとどまっています。{self.owner}様子を確認してください。"
            + (f" 検知 {time}" if time else ""),
            early_alert=True,
            path=_clip_notice_path(clip_id),
        )

    def send_assessment(
        self,
        label: str,
        *,
        location: str | None = None,
        interrupted: bool = False,
        clip_id: str = "",
        detected_at: str | None = None,
        existing_stool: bool = False,
        stool_condition: str | None = None,
        stool_repeat: int = 0,
    ) -> bool:
        if label not in ("feces", "urine", "both"):
            return False
        place = "トレー外での" if location == "outside" else ""
        detail = (
            "録画に一部欠落があります。動画を確認してください。"
            if interrupted
            else "動画を確認できます。"
        )
        time = _notice_time(detected_at)
        if existing_stool:
            message = f"トレーの映像で便を確認しました。排便時刻は不明です。{detail}"
            if time:
                message += f" 便を確認 {time}"
        elif stool_condition == "diarrhea":
            again = f"24時間で{stool_repeat}回目です。" if stool_repeat > 1 else ""
            message = (
                f"動画の自動判定で{place}下痢らしい便の候補が見つかりました。{again}{detail}"
                "続くときは獣医師に相談してください。"
            )
            if time:
                message += f" 検知 {time}"
        else:
            message = (
                f"動画の自動判定で{place}{EXCRETION_TYPES[label]}の候補が見つかりました。{detail}"
            )
            if time:
                message += f" 検知 {time}"
        path = _clip_notice_path(clip_id)
        return self._display(message, path=path)

    def send_sheet_due(self, events: int, ratio: float) -> bool:
        """前回の交換からの排泄の回数か、汚れの割合が上限に達したことを知らせる。"""
        count = f"排泄{events}回、" if events else ""
        message = (
            f"{self.owner}トイレのシートがそろそろ交換どきです。"
            f"前回の交換から{count}汚れは監視範囲の約{ratio:.0%}です。"
        )
        return self._display(message, path="/")

    def send_floor_object(self, object_id: str, detected_at: str) -> bool:
        """床に新しく現れて動かない物の候補を知らせる（実験的）。"""
        time = _notice_time(detected_at)
        message = (
            f"{self.owner}サークルの床に、見慣れない物があります。"
            "異物や嘔吐の跡かもしれません。画像を確認してください。"
            + (f" 検知 {time}" if time else "")
        )
        return self._display(message, path="/floor?" + urlencode({"id": object_id}))

    def send_trend(self, labels: list[str], day: date) -> bool:
        """移動・睡眠・排泄の回数が、2日続けていつもと違うことを知らせる。"""
        message = (
            f"{self.owner}様子が2日続けていつもと違います（{'、'.join(labels)}）。"
            "記録を見返し、気になるときは獣医師に相談してください。"
        )
        return self._display(message, path="/trends?" + urlencode({"day": day.isoformat()}))

    def send_floor_judgement(self, label: str, object_id: str, repeat: int) -> bool:
        """判定器が床の候補を嘔吐・下痢らしい跡としたことを知らせる。repeat は24時間での回数。"""
        again = f"24時間で{repeat}回目です。" if repeat > 1 else ""
        message = (
            f"{self.owner}サークルの床に、{label}らしい跡があります（判定器による）。"
            f"{again}画像を確かめ、続くときは獣医師に相談してください。"
        )
        return self._display(message, path="/floor?" + urlencode({"id": object_id}))

    def send_vocal(self, loud_seconds: float, minutes: int) -> bool:
        """外出中に、minutes 分のうち loud_seconds 秒ほど鳴いていたことを知らせる。"""
        loud = (
            f"約{max(1, round(loud_seconds / 60))}分"
            if loud_seconds >= 60
            else f"約{round(loud_seconds)}秒"
        )
        message = (
            f"外出中に、{self.owner}鳴き声らしい音がこの{minutes}分で{loud}続いています。"
            "ライブ映像で様子を確かめてください。"
        )
        return self._display(message, path="/vocal")

    def send_away_reminder(self, hours: float) -> bool:
        """外出中にしてから hours 時間たったことを知らせる（在宅に戻し忘れの確認）。"""
        message = (
            f"外出中にしてから{hours:g}時間たちました。帰宅していたら、"
            "ホームで「在宅に戻す」を押してください。"
        )
        return self._display(message, path="/")

    def send_absence(self, since: datetime, minutes: float) -> bool:
        """外出中に、犬がサークルにいない時間が続いていることを知らせる。"""
        start = since.astimezone().strftime("%H:%M")
        message = (
            f"外出中ですが、{self.owner}姿が{start}から約{minutes:.0f}分サークルに見えません。"
            "脱走していないか確かめてください。"
        )
        return self._display(message, path="/presence")

    def send_return(self, minutes: float) -> bool:
        """外出中に知らせた不在のあと、サークルに戻ったことを知らせる。"""
        return self._display(
            f"{self.owner}姿がサークルに戻りました。約{minutes:.0f}分見えませんでした。",
            path="/presence",
        )

    def send_reentry(self, kind: str, event_id: str, at: datetime, clip_id: str | None) -> Delivery:
        detail, risk = {
            "reentry": ("未回収の排泄候補があるトレーに再侵入", "MEDIUM"),
            "high": ("排泄物を踏んだ可能性が高い", "HIGH"),
            "uncollected": ("排泄候補の片付けが未確認", "要確認"),
        }[kind]
        path = (
            ("/clips?" + urlencode({"id": clip_id}))
            if clip_id
            else ("/records/events?" + urlencode({"id": event_id}))
        )
        message = (
            f"{at:%Y-%m-%d %H:%M:%S %Z} [{risk}] {detail}。"
            f"映像とトレーを確認し、必要に応じて片付けてください。記録ID: {event_id}"
        )
        return self._deliver_reentry(message, path) if self.enabled else "skipped"

    def _deliver_reentry(self, message: str, path: str) -> Delivery:
        return "sent" if self._display(message, path=path) else "unknown"

    def send_outage(self, kind: str, since: datetime) -> bool:
        """監視の停止（monitor）、カメラの不調（camera）、判定器の停止（judge）を知らせる。

        since は始まった時刻。
        """
        start = since.astimezone().strftime("%m/%d %H:%M")
        if kind == "monitor":
            message = (
                f"{self.owner}トイレの監視が{start}から止まっています。"
                "監視するマシンを確認してください。"
            )
        elif kind == "judge":
            # 差分だけの通知は抑えたままなので、届かなくなる通知を伝える。
            message = (
                f"{start}から動画の自動判定が失敗し続けています。この間、排泄の通知は届きません。"
                "判定器の利用上限とログインを確認してください。"
            )
        else:
            message = (
                f"{start}からカメラの映像が届いていません。"
                "カメラの電源とネットワークを確認してください。"
            )
        return self.enabled and self._deliver_watchdog(message)

    def send_recovery(self, kind: str, seconds: float) -> bool:
        """停止からの復旧を、止まっていた長さと合わせて知らせる。"""
        duration = format_duration(seconds)
        if kind == "monitor":
            message = f"{self.owner}トイレの監視が再開しました。{duration}止まっていました。"
        elif kind == "judge":
            message = (
                f"動画の自動判定が再開しました。{duration}止まっていました。"
                "この間の動画を順に判定します。"
            )
        else:
            message = f"カメラの映像が戻りました。{duration}届いていませんでした。"
        return self.enabled and self._deliver_watchdog(message)

    def _display(self, message: str, *, early_alert: bool = False, path: str = "") -> bool:
        return self.enabled and self._deliver(message, early_alert=early_alert, path=path)

    def _deliver_watchdog(self, message: str) -> bool:
        """監視の見張りの通知を送る。既定ではトイレの通知と同じ送り先へ送る。"""
        return self._deliver(message, early_alert=False)

    def _deliver(self, message: str, *, early_alert: bool, path: str = "") -> bool:
        """通知を1件送り、受け付けられたら True を返す。"""
        raise NotImplementedError


class FanOutNotifier(ToiletNotifier):
    """同じ通知を複数の送信先（ntfy・macOS と Web Push など）へ送る。

    文面と有効・無効は送信先ごとに持つ。どれか1つでも受け付けられれば成功とする。
    """

    def __init__(self, notifiers: Sequence[ToiletNotifier]) -> None:
        super().__init__(any(notifier.enabled for notifier in notifiers))
        self.notifiers = tuple(notifiers)

    def _deliver(self, message: str, *, early_alert: bool, path: str = "") -> bool:
        results = [
            notifier._display(message, early_alert=early_alert, path=path)
            for notifier in self.notifiers
        ]
        return any(results)

    def _deliver_watchdog(self, message: str) -> bool:
        results = [
            notifier.enabled and notifier._deliver_watchdog(message) for notifier in self.notifiers
        ]
        return any(results)

    def _deliver_reentry(self, message: str, path: str) -> Delivery:
        return merge_delivery(
            [
                notifier._deliver_reentry(message, path)
                for notifier in self.notifiers
                if notifier.enabled
            ]
        )


def applescript_string(value: str) -> str:
    """AppleScript の二重引用符の文字列として安全に埋め込めるようにする。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class MacNotifier(ToiletNotifier):
    """osascript で macOS の通知センターに通知する。"""

    def __init__(
        self,
        enabled: bool = True,
        runner: Callable[..., Any] = subprocess.run,
        *,
        pet_name: str = "",
    ) -> None:
        super().__init__(enabled, pet_name)
        self.runner = runner

    # macOS の通知は見守り用の送り先を分けられないため、early_alert は使わない。
    def _deliver(self, message: str, *, early_alert: bool, path: str = "") -> bool:  # noqa: ARG002
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


def validated_ntfy_server(server: str) -> str:
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


class NtfyNotifier(ToiletNotifier):
    """ntfy サーバーへ HTTP POST で通知する。"""

    def __init__(
        self,
        topic: str,
        enabled: bool = True,
        opener: Callable[..., Any] | None = None,
        *,
        early_topic: str = "",
        pet_name: str = "",
        server: str = DEFAULT_NTFY_SERVER,
        public_url: str = "",
    ) -> None:
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
        self.public_url = public_url.rstrip("/")
        self.opener: Callable[..., Any] = urlopen if opener is None else opener

    def _deliver(self, message: str, *, early_alert: bool, path: str = "") -> bool:
        link = self.public_url + path if self.public_url and path else ""
        return self._publish(message, early_alert=early_alert, link=link)

    def _deliver_reentry(self, message: str, path: str) -> Delivery:
        link = self.public_url + path if self.public_url and path else ""
        return self._publish_result(message, early_alert=False, link=link)

    def _publish(self, message: str, *, early_alert: bool, link: str = "") -> bool:
        return self._publish_result(message, early_alert=early_alert, link=link) == "sent"

    def _publish_result(self, message: str, *, early_alert: bool, link: str = "") -> Delivery:
        separate = early_alert and bool(self.early_topic)
        topic = self.early_topic if separate else self.topic
        data = {"topic": topic, "title": self.title(early_alert=separate), "message": message}
        if link:
            data["click"] = link
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        # 送信先は validated_ntfy_server で http(s) だけに限っている。
        request = Request(  # noqa: S310
            self.server,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=5) as response:
                return "sent" if 200 <= response.status < 300 else "unknown"
        except HTTPError as error:
            logger.warning("ntfyに通知を送信できませんでした（状態 %s）", error.code)
            return "retry" if error.code in (429, 503) else "unknown"
        except OSError:
            logger.warning("ntfyに通知を送信できませんでした")
            return "unknown"
