"""GUI の稼働中、カメラの有無に関わらず給餌予定を通知する。"""

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from ..records.database import Database
from ..records.feeding_plans import claim_due
from ..timezones import local_now
from ..web_push import WebPushNotifier

logger = logging.getLogger(__name__)


def notify_due(database: Database, notifier: WebPushNotifier, now: datetime) -> None:
    recipients = notifier.subscriptions.recipients(feeding=True)
    if recipients:
        for clock in claim_due(database, now):
            notifier.send_feeding_reminder(recipients, clock)


@contextmanager
def feeding_reminders(database: Database, notifier: WebPushNotifier) -> Iterator[None]:
    stop = threading.Event()

    def run() -> None:
        while not stop.wait(15):
            try:
                notify_due(database, notifier, local_now())
            except (OSError, sqlite3.Error, ValueError):
                logger.warning("ごはんの予定を知らせる処理で問題が起きました")

    worker = threading.Thread(target=run, daemon=True, name="feeding-reminders")
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=1)
