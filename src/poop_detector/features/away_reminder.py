"""外出中にしてから長い時間たったら、在宅に戻し忘れていないかを一度だけ知らせる。"""

import logging
from datetime import datetime, timedelta
from typing import Protocol

from ..records.away_periods import AwayPeriods

logger = logging.getLogger(__name__)


class ReminderNotifier(Protocol):
    """押し忘れを知らせる通知先。"""

    def send_away_reminder(self, hours: float) -> bool: ...


class AwayReminder:
    """GUI のサーバーの見張りから check を呼ばれる。hours が 0 なら知らせない。"""

    def __init__(self, periods: AwayPeriods, notifier: ReminderNotifier, hours: float) -> None:
        self.periods, self.notifier, self.hours = periods, notifier, hours

    def check(self, now: datetime) -> None:
        if self.hours <= 0:
            return
        period = self.periods.current()
        if period is None or period.reminded:
            return
        if now - period.started_at < timedelta(hours=self.hours):
            return
        # 送れなかったときは残さず、次の確認でまた試す。
        if self.notifier.send_away_reminder(self.hours):
            self.periods.mark_reminded(period.id)
            logger.info("外出中のままになっていることを知らせました")
