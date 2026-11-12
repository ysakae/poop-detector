"""Web Push で、GUI で購読した人の端末に通知を送る。

本文は RFC 8291（aes128gcm）で暗号化し（http-ece）、RFC 8292 の VAPID で署名する（py-vapid）。
送信は urllib の POST で、宛先は既知のプッシュサービス（Google・Mozilla・Apple・Microsoft）に
限る。購読はログインした人が登録するので、内部のアドレスへ送らせないため（SSRF の対策）。
本文に映像や URL の秘密は入れず、開く画面の相対パスだけを入れる。
"""

import base64
import binascii
import fcntl
import json
import logging
import os
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import http_ece
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

from .notifications import ToiletNotifier
from .records.database import Database

logger = logging.getLogger(__name__)
KEY_FILE = ".web-push-vapid.pem"
# 宛先として受け付けるプッシュサービスのドメイン（この名前か、そのサブドメイン）。
PUSH_SERVICES = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)
ENDPOINT_MAX_LENGTH = 2048
# 端末がオフラインでも、この秒数のあいだはプッシュサービスが通知を預かる。
TTL_SECONDS = 3600
SEND_TIMEOUT_SECONDS = 5
GONE = (404, 410)
# 監視の異常の通知を開いたときの画面（管理 › 稼働状況とトレー）。
WATCHDOG_URL = "/manage"


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def valid_endpoint(endpoint: str) -> bool:
    """既知のプッシュサービスの https の宛先か。"""
    if len(endpoint) > ENDPOINT_MAX_LENGTH:
        return False
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        return False
    return any(host == service or host.endswith("." + service) for service in PUSH_SERVICES)


def valid_key(value: str, size: int) -> bool:
    """ブラウザーが渡す鍵（base64url）が、決まった長さか。"""
    try:
        return len(_b64decode(value)) == size
    except (binascii.Error, ValueError):
        return False


class PushKeys:
    """VAPID の鍵。データの保存先に1つだけ作り、監視と GUI で共有する（本人だけが読める）。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.path = self.root / KEY_FILE

    def vapid(self) -> Vapid02:
        if not self.path.is_file():
            self._create()
        return Vapid02.from_file(str(self.path))

    def _create(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".web-push.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.is_file():
                return
            pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            pending = self.root / f".{KEY_FILE}.tmp"
            descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(pem)
            pending.replace(self.path)

    def public_key(self) -> str:
        """ブラウザーの購読に渡す公開鍵（applicationServerKey、base64url）。"""
        public = self.vapid().public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return _b64encode(public)


@dataclass(frozen=True)
class Subscription:
    """1台の端末のブラウザーの購読。"""

    endpoint: str
    user_id: str
    p256dh: str
    auth: str


class PushSubscriptions:
    """push_subscriptions テーブル。端末（endpoint）ごとに1行で、持ち主は最後に登録した人。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, user_id: str, endpoint: str, p256dh: str, auth: str) -> None:
        if not valid_endpoint(endpoint) or not valid_key(p256dh, 65) or not valid_key(auth, 16):
            raise ValueError("通知の購読の内容を確認してください")
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with self.database.connect() as db:
            db.execute(
                """INSERT INTO push_subscriptions (endpoint, user_id, p256dh, auth, created_at)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET
                user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth,
                created_at = excluded.created_at""",
                (endpoint, user_id, p256dh, auth, stamp),
            )

    def remove(self, user_id: str, endpoint: str) -> bool:
        """自分の端末の購読をやめる。ほかの人の購読は消さない。"""
        with self.database.connect() as db:
            removed = db.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
                (endpoint, user_id),
            ).rowcount
        return bool(removed)

    def expire(self, endpoint: str) -> None:
        """プッシュサービスが無効と答えた購読を消す。"""
        with self.database.connect() as db:
            db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

    def for_user(self, user_id: str) -> list[Subscription]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM push_subscriptions WHERE user_id = ? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        return [self._subscription(row) for row in rows]

    def recipients(
        self, *, early_alert: bool = False, watchdog: bool = False
    ) -> list[Subscription]:
        """通知を受け取る有効な人の購読。見守り通知は、見守りも選んだ人だけ。

        watchdog（監視の停止とカメラの不調）は、受け取りを選んだ管理者だけ。
        """
        if watchdog:
            condition = "u.role = 'admin' AND u.push_watchdog = 1"
        else:
            condition = "u.push_early = 1" if early_alert else "u.push_alerts = 1"
        with self.database.connect() as db:
            rows = db.execute(
                # 条件は上の固定の文字列のどれかで、利用者の入力ではない。
                "SELECT s.* FROM push_subscriptions s JOIN users u ON u.id = s.user_id "  # noqa: S608
                f"WHERE u.disabled_at IS NULL AND {condition} ORDER BY s.created_at"
            ).fetchall()
        return [self._subscription(row) for row in rows]

    @staticmethod
    def _subscription(row: Any) -> Subscription:
        return Subscription(row["endpoint"], row["user_id"], row["p256dh"], row["auth"])


def push_request(
    subscription: Subscription, payload: bytes, vapid: Vapid02, subject: str
) -> Request:
    """暗号化と署名をした、プッシュサービスへの POST の要求。"""
    body = http_ece.encrypt(
        payload,
        salt=os.urandom(16),
        private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=_b64decode(subscription.p256dh),
        auth_secret=_b64decode(subscription.auth),
        version="aes128gcm",
    )
    parts = urlsplit(subscription.endpoint)
    claims = {
        "sub": subject,
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(time.time()) + 12 * 60 * 60,
    }
    headers = {
        **vapid.sign(claims),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(TTL_SECONDS),
        "Urgency": "high",
    }
    # 宛先は valid_endpoint で既知のプッシュサービスの https に限っている。
    return Request(subscription.endpoint, data=body, headers=headers, method="POST")  # noqa: S310


class WebPushNotifier(ToiletNotifier):
    """購読した人の端末へ Web Push で通知する。通知を開くと url の画面に移る。"""

    def __init__(
        self,
        subscriptions: PushSubscriptions,
        keys: PushKeys,
        *,
        subject: str,
        enabled: bool = True,
        pet_name: str = "",
        url: str = "/review",
        opener: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(enabled, pet_name)
        self.subscriptions = subscriptions
        self.keys = keys
        self.subject = subject
        self.url = url
        self.opener: Callable[..., Any] = urlopen if opener is None else opener

    def _deliver(self, message: str, *, early_alert: bool) -> bool:
        recipients = self.subscriptions.recipients(early_alert=early_alert)
        return self._push(recipients, self.title(early_alert=early_alert), message, self.url)

    def _deliver_watchdog(self, message: str) -> bool:
        """監視の異常は管理者だけに送り、開くと管理の稼働状況に移る。"""
        recipients = self.subscriptions.recipients(watchdog=True)
        return self._push(recipients, self.title(), message, WATCHDOG_URL)

    def _push(self, recipients: list[Subscription], title: str, message: str, url: str) -> bool:
        if not recipients:
            return False
        payload = json.dumps(
            {"title": title, "body": message, "url": url}, ensure_ascii=False
        ).encode("utf-8")
        vapid = self.keys.vapid()
        results = [self.send_to(subscription, payload, vapid) for subscription in recipients]
        return any(results)

    def send_test_to(self, subscriptions: list[Subscription]) -> int:
        """指定の端末にテストの通知を送り、受け付けられた台数を返す。"""
        payload = json.dumps(
            {
                "title": self.title(),
                "body": f"{self.owner}トイレの通知テストです。",
                "url": self.url,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        vapid = self.keys.vapid()
        return sum(self.send_to(subscription, payload, vapid) for subscription in subscriptions)

    def send_to(self, subscription: Subscription, payload: bytes, vapid: Vapid02) -> bool:
        """1台に送る。無効になった購読（404・410）は台帳から消す。宛先はログに出さない。"""
        request = push_request(subscription, payload, vapid, self.subject)
        try:
            with self.opener(request, timeout=SEND_TIMEOUT_SECONDS) as response:
                return bool(200 <= response.status < 300)
        except urllib.error.HTTPError as error:
            if error.code in GONE:
                self.subscriptions.expire(subscription.endpoint)
                logger.info("無効になった Web Push の購読を削除しました")
            else:
                logger.warning("Web Push を送信できませんでした（状態 %s）", error.code)
            return False
        except OSError:
            logger.warning("Web Push を送信できませんでした")
            return False


def push_subject(public_url: str) -> str:
    """VAPID の連絡先（sub）。外部の URL があればそれを使う。"""
    return public_url.rstrip("/") if public_url else "mailto:poop-detector@localhost"
