"""在宅に戻したことと、外出中の押し忘れを家族に知らせる（#293）を検証する。"""

from datetime import datetime, timedelta

import pytest

from poop_detector.config import Config, ConfigError
from poop_detector.features.away_reminder import AwayReminder
from poop_detector.notifications import ToiletNotifier
from poop_detector.records.absences import AwayMode
from poop_detector.records.away_periods import AwayPeriods
from poop_detector.records.database import events_database
from poop_detector.web_push import PushKeys, PushSubscriptions, WebPushNotifier

ENDPOINT = "https://fcm.googleapis.com/fcm/send/"


def _device_key():
    """暗号化できる本物の端末の公開鍵（P-256 の非圧縮形式）を作る。"""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    public = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return base64.urlsafe_b64encode(public).rstrip(b"=").decode()


P256DH = _device_key()
AUTH = "YWFhYWFhYWFhYWFhYWFhYQ"


# --- 押し忘れ ---


class Notifier:
    def __init__(self, accept=True):
        self.accept = accept
        self.sent = []

    def send_away_reminder(self, hours):
        self.sent.append(hours)
        return self.accept


def reminder(tmp_path, notifier, hours=12):
    database = events_database(tmp_path)
    return AwayReminder(AwayPeriods(database), notifier, hours), AwayMode(database)


def test_reminder_is_sent_once_after_the_hours(tmp_path):
    notifier = Notifier()
    check, away = reminder(tmp_path, notifier)
    away.set_away(True)
    now = datetime.now().astimezone()
    check.check(now + timedelta(hours=11))
    assert notifier.sent == []
    check.check(now + timedelta(hours=12, minutes=1))
    check.check(now + timedelta(hours=13))
    assert notifier.sent == [12]


def test_reminder_is_retried_when_it_could_not_be_sent(tmp_path):
    notifier = Notifier(accept=False)
    check, away = reminder(tmp_path, notifier)
    away.set_away(True)
    later = datetime.now().astimezone() + timedelta(hours=13)
    check.check(later)
    check.check(later)
    assert len(notifier.sent) == 2


def test_reminder_does_nothing_at_home_or_when_disabled(tmp_path):
    notifier = Notifier()
    check, _ = reminder(tmp_path, notifier)
    check.check(datetime.now().astimezone() + timedelta(days=2))
    off, away2 = reminder(tmp_path / "off", notifier, hours=0)
    away2.set_away(True)
    off.check(datetime.now().astimezone() + timedelta(days=2))
    assert notifier.sent == []


def test_next_away_period_can_be_reminded_again(tmp_path):
    notifier = Notifier()
    check, away = reminder(tmp_path, notifier)
    away.set_away(True)
    later = datetime.now().astimezone() + timedelta(hours=13)
    check.check(later)
    away.set_away(False)
    away.set_away(True)
    check.check(later + timedelta(hours=13))
    assert len(notifier.sent) == 2


# --- 送り先 ---


def subscribe(database, name, *, household=True):
    from poop_detector.gui.accounts import Accounts

    accounts = Accounts(database)
    user_id = accounts.users.create(name, "member")
    accounts.users.set_push(user_id, alerts=True, early=False, household=household)
    PushSubscriptions(database).add(user_id, ENDPOINT + name, P256DH, AUTH)
    return user_id


def test_household_recipients_exclude_the_person_who_returned(tmp_path):
    database = events_database(tmp_path)
    me = subscribe(database, "わたし")
    other = subscribe(database, "家族")
    subscribe(database, "受け取らない人", household=False)
    recipients = PushSubscriptions(database).recipients(household=True, exclude_user=me)
    assert [r.user_id for r in recipients] == [other]


def test_send_return_home_pushes_to_other_family(tmp_path):
    database = events_database(tmp_path)
    me = subscribe(database, "わたし")
    subscribe(database, "家族")
    sent = []

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(request, timeout):
        sent.append(request.full_url)
        return Response()

    notifier = WebPushNotifier(
        PushSubscriptions(database),
        PushKeys(tmp_path),
        subject="mailto:a@example.com",
        opener=opener,
    )
    assert notifier.send_return_home("わたし", "abc", exclude_user=me)
    assert sent == [ENDPOINT + "家族"]


# --- 文面と設定 ---


class Messages(ToiletNotifier):
    def __init__(self):
        super().__init__(True, "ポチ")
        self.messages = []

    def _deliver(self, message, *, early_alert, path=""):
        self.messages.append((message, path))
        return True


def test_away_reminder_message():
    notifier = Messages()
    assert notifier.send_away_reminder(12)
    message, path = notifier.messages[0]
    assert "12時間" in message
    assert "在宅に戻す" in message
    assert path == "/"


def test_away_reminder_setting():
    assert Config().away_reminder_hours == 12
    with pytest.raises(ConfigError, match=r"away\.reminder_hours"):
        Config(away_reminder_hours=-1)


# --- 画面 ---


def test_returning_home_from_the_gui_notifies_other_family(gui, monkeypatch):
    from support import submit

    from poop_detector.gui import presence_routes

    server, store, _ = gui
    calls = []
    monkeypatch.setattr(
        presence_routes,
        "notify_return",
        lambda app, user, period_id: calls.append((user.name if user else None, period_id)),
    )
    submit(server, "/away", {"away": "1"}, page="/")
    period = AwayPeriods(store.database).current()
    submit(server, "/away", {"away": "0"}, page="/")
    assert len(calls) == 1
    assert calls[0][1] == period.id
    submit(server, "/away", {"away": "0"}, page="/")  # 在宅のまま押し直しても知らせない
    assert len(calls) == 1


def test_account_page_has_the_household_preference(gui):
    from support import exchange, login, submit

    server, _, _ = gui
    cookie = login(server, role="member")
    _, _, body = exchange(server, "GET", "/account", headers=cookie, auth=False)
    assert "家族が在宅に戻したときに知らせる" in body.decode()
    status, _ = submit(server, "/account/push/preferences", {"alerts": "1"}, page="/account")
    assert status == 303


def test_away_reminder_watch_runs_only_when_enabled(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from poop_detector.commands.context import CommandContext, away_reminder_watch

    checks = []

    def watching(check, interval):
        checks.append(interval)
        check()
        return nullcontext()

    monkeypatch.setattr("poop_detector.features.watchdog.watching", watching)
    notifier = Notifier()
    off = CommandContext(Config(data_dir=str(tmp_path), away_reminder_hours=0))
    with away_reminder_watch(off, notifier):
        pass
    assert checks == []
    with away_reminder_watch(CommandContext(Config(data_dir=str(tmp_path))), notifier):
        pass
    assert len(checks) == 1
