"""GUI の稼働中、カメラの有無に関わらず給餌予定を通知する。"""

import hashlib
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from ..records.database import Database
from ..records.feeding_plans import claim_due, retry_reminder
from ..timezones import local_now
from ..web_push import WebPushNotifier

logger = logging.getLogger(__name__)


def notify_due(database: Database, notifier: WebPushNotifier, now: datetime) -> None:
    for subscription in notifier.subscriptions.recipients(feeding=True):
        recipient = hashlib.sha256(subscription.endpoint.encode()).hexdigest()
        for reminder in claim_due(database, now, recipient):
            clock = reminder.clock
            if reminder.day != now.astimezone().date():
                clock = f"きのう {clock}"
            if notifier.send_feeding_reminder(subscription, clock, reminder.day) == "retry":
                retry_reminder(database, reminder, recipient, now)


@contextmanager
def feeding_reminders(database: Database, notifier: WebPushNotifier) -> Iterator[None]:
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            try:
                notify_due(database, notifier, local_now())
            except (OSError, sqlite3.Error, ValueError):
                logger.warning("ごはんの予定を知らせる処理で問題が起きました")
            stop.wait(15)

    worker = threading.Thread(target=run, daemon=True, name="feeding-reminders")
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=1)
