"""Web Push の送信（暗号化と VAPID の署名、購読の台帳、送信先の選び方。#89）を検証する。

送信は差し替え用の opener で受け取り、テストで作った受信側の鍵で復号して中身を確かめる。
"""

import base64
import json
import stat
import urllib.error
from types import SimpleNamespace

import http_ece
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from poop_detector.gui.accounts import Accounts
from poop_detector.notifications import FanOutNotifier, ToiletNotifier
from poop_detector.records.database import events_database
from poop_detector.web_push import (
    KEY_FILE,
    PushKeys,
    PushSubscriptions,
    WebPushNotifier,
    valid_endpoint,
)

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Receiver:
    """ブラウザーの購読の代わり。受け取った本文を自分の鍵で復号する。"""

    def __init__(self, endpoint=ENDPOINT):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"0123456789abcdef"
        self.endpoint = endpoint
        public = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.p256dh = b64(public)
        self.auth_b64 = b64(self.auth)

    def decrypt(self, body):
        data = http_ece.decrypt(
            body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"
        )
        return json.loads(data)


class Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def opener(status=201):
    requests = []

    def open_(request, timeout):
        requests.append((request, timeout))
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "gone", {}, None)
        return Response(status)

    open_.requests = requests
    return open_


@pytest.fixture
def push(tmp_path):
    database = events_database(tmp_path)
    accounts = Accounts(database)
    return SimpleNamespace(
        root=tmp_path,
        users=accounts.users,
        subscriptions=PushSubscriptions(database),
        keys=PushKeys(tmp_path),
    )


def test_vapid_key_is_created_once_and_kept_private(push):
    public = push.keys.public_key()
    assert len(base64.urlsafe_b64decode(public + "=")) == 65
    path = push.root / KEY_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert PushKeys(push.root).public_key() == public


@pytest.mark.parametrize(
    ("endpoint", "ok"),
    [
        ("https://fcm.googleapis.com/fcm/send/x", True),
        ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
        ("https://web.push.apple.com/QGx", True),
        ("https://wns2-par02p.notify.windows.com/w/?token=x", True),
        ("http://fcm.googleapis.com/fcm/send/x", False),
        ("https://127.0.0.1/send", False),
        ("https://intranet.local/x", False),
        ("https://fcm.googleapis.com.evil.example/x", False),
        ("https://user:pass@fcm.googleapis.com/x", False),
        ("https://fcm.googleapis.com/" + "x" * 3000, False),
    ],
)
def test_only_known_push_services_are_accepted(endpoint, ok):
    """ログインした人が登録する宛先なので、内部のアドレスへ送らせない（SSRF の対策）。"""
    assert valid_endpoint(endpoint) is ok


def subscribe(push, name, receiver, *, alerts=True, early=False):
    user_id = push.users.create(name, "member")
    push.users.set_push(user_id, alerts=alerts, early=early)
    push.subscriptions.add(user_id, receiver.endpoint, receiver.p256dh, receiver.auth_b64)
    return user_id


def notifier(push, send):
    return WebPushNotifier(
        push.subscriptions,
        push.keys,
        subject="https://toilet.example.ts.net",
        pet_name="ポチ",
        opener=send,
    )


def test_notification_is_encrypted_and_signed_for_each_subscriber(push):
    receiver = Receiver()
    subscribe(push, "家族A", receiver)
    send = opener()
    assert notifier(push, send).send()
    ((request, timeout),) = send.requests
    assert request.full_url == ENDPOINT
    assert timeout <= 10
    assert request.get_header("Content-encoding") == "aes128gcm"
    assert request.get_header("Ttl")
    authorization = request.get_header("Authorization")
    assert authorization.startswith("vapid t=")
    assert f"k={push.keys.public_key()}" in authorization
    payload = receiver.decrypt(request.data)
    assert payload == {
        "title": "ポチのトイレ",
        "body": "ポチのトイレに変化を検出しました。排便・排尿の可能性があります。",
        "url": "/review",
    }


def test_early_alerts_go_only_to_those_who_want_them(push):
    alerts, early, none, disabled = (Receiver(f"{ENDPOINT}-{n}") for n in range(4))
    subscribe(push, "通知だけ", alerts)
    subscribe(push, "見守りも", early, early=True)
    subscribe(push, "受け取らない", none, alerts=False)
    gone = subscribe(push, "無効な人", disabled, early=True)
    push.users.create("管理者", "admin")
    push.users.disable(gone)
    send = opener()
    assert notifier(push, send).send_early_alert()
    assert [request.full_url for request, _ in send.requests] == [early.endpoint]
    send = opener()
    notifier(push, send).send()
    assert {request.full_url for request, _ in send.requests} == {alerts.endpoint, early.endpoint}


def test_expired_subscriptions_are_removed(push):
    receiver = Receiver()
    user_id = subscribe(push, "家族A", receiver)
    assert not notifier(push, opener(410)).send()
    assert push.subscriptions.for_user(user_id) == []


def test_temporary_failures_keep_the_subscription(push):
    receiver = Receiver()
    user_id = subscribe(push, "家族A", receiver)
    assert not notifier(push, opener(500)).send()
    assert len(push.subscriptions.for_user(user_id)) == 1


def test_disabled_notifications_send_nothing(push):
    subscribe(push, "家族A", Receiver())
    send = opener()
    quiet = WebPushNotifier(
        push.subscriptions, push.keys, subject="https://x.ts.net", enabled=False, opener=send
    )
    assert not quiet.send()
    assert send.requests == []


def test_subscribing_again_moves_the_device_to_the_new_user(push):
    receiver = Receiver()
    first = subscribe(push, "家族A", receiver)
    second = push.users.create("家族B", "member")
    push.subscriptions.add(second, receiver.endpoint, receiver.p256dh, receiver.auth_b64)
    assert push.subscriptions.for_user(first) == []
    assert len(push.subscriptions.for_user(second)) == 1
    push.subscriptions.remove(second, receiver.endpoint)
    assert push.subscriptions.for_user(second) == []


class Recording(ToiletNotifier):
    def __init__(self, result, enabled=True):
        super().__init__(enabled)
        self.result = result
        self.messages = []

    def _deliver(self, message, *, early_alert):
        self.messages.append((message, early_alert))
        return self.result


def test_fan_out_sends_through_every_channel():
    failing, working = Recording(False), Recording(True)
    both = FanOutNotifier([failing, working])
    assert both.send_early_alert()
    assert failing.messages == working.messages
    assert failing.messages[0][1] is True
    assert not FanOutNotifier([Recording(True, enabled=False)]).enabled


def test_v8_database_gains_push_tables_and_defaults(tmp_path):
    import sqlite3

    from poop_detector.records.database import MIGRATIONS

    with sqlite3.connect(tmp_path / "events.sqlite3") as db:
        for migration in MIGRATIONS[:8]:
            migration(db)
        db.execute("PRAGMA user_version = 8")
        db.execute(
            "INSERT INTO users (id, name, role, created_at) VALUES ('old', '家族A', 'admin', '2026')"
        )
    database = events_database(tmp_path)
    user = Accounts(database).users.get("old")
    assert (user.push_alerts, user.push_early) == (True, False)
    assert PushSubscriptions(database).for_user("old") == []


def test_monitoring_sends_through_web_push_too(tmp_path):
    from poop_detector.commands.context import CommandContext, with_web_push
    from poop_detector.config import Config

    primary = Recording(True)
    context = CommandContext(Config(data_dir=str(tmp_path), pet_name="ポチ"))
    combined = with_web_push(primary, context)
    assert isinstance(combined, FanOutNotifier)
    push = combined.notifiers[1]
    assert isinstance(push, WebPushNotifier)
    assert push.subject == "mailto:poop-detector@localhost"
    assert combined.send()
    assert primary.messages
