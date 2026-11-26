"""活動量・睡眠・排泄の回数の傾向と、いつもとの違い（#271）を検証する。"""

from datetime import date, datetime, time, timedelta

import pytest

from poop_detector.features.activity_map import ActivityStore
from poop_detector.features.daily_trends import (
    METRICS,
    MIN_DAYS,
    TrendNotices,
    deviation,
    gather,
    unusual,
)
from poop_detector.features.sleep_store import MinuteRecord, SleepStore
from poop_detector.records.events import Store

DAY = date(2026, 11, 20)


def series(values, last=DAY):
    """last に向かって並べた値を、日付ごとの辞書にする。"""
    start = last - timedelta(days=len(values) - 1)
    return {start + timedelta(days=i): value for i, value in enumerate(values)}


# --- いつもとの違い ---


def test_deviation_flags_a_clear_drop():
    values = series([200, 220, 180, 200, 240, 200, 220, 180, 200, 200, 220, 200, 180, 200, 40])
    result = deviation(values, DAY, "moves")
    assert result is not None
    assert result.direction == "low"
    assert result.usual == 200
    assert result.value == 40


def test_deviation_ignores_ordinary_variation():
    values = series([200, 220, 180, 200, 240, 200, 220, 180, 200, 200, 220, 200, 180, 200, 260])
    assert deviation(values, DAY, "moves") is None


def test_deviation_needs_enough_usual_days():
    values = series([10] * (MIN_DAYS - 1) + [100])
    assert deviation(values, DAY, "moves") is None
    values = series([10] * MIN_DAYS + [100])
    assert deviation(values, DAY, "moves").direction == "high"


def test_missing_days_are_skipped_and_missing_value_is_not_judged():
    values = series([10, None, 10, None, 10, 10, 10, 10, 10, 10, None])
    assert deviation(values, DAY, "moves") is None
    values[DAY] = 60
    assert deviation(values, DAY, "moves").direction == "high"


def test_small_counts_use_the_minimum_spread():
    # いつも1日2回の排便が1回になっただけでは知らせない。
    values = series([2] * 14 + [1])
    assert deviation(values, DAY, "feces") is None
    values = series([3] * 14 + [0])
    assert deviation(values, DAY, "feces").direction == "low"


def test_every_metric_has_label_and_unit():
    assert set(METRICS) == {"moves", "sleep", "feces", "urine"}
    for metric in METRICS.values():
        assert metric.label
        assert metric.unit
        assert metric.min_spread > 0


# --- 集める ---


def local(day, hour=12, minute=0):
    return datetime.combine(day, time(hour, minute)).astimezone()


def test_gather_counts_records_sleep_and_moves(tmp_path):
    store = Store(tmp_path)
    store.add_excretion("feces", local(DAY, 8).isoformat())
    store.add_excretion("both", local(DAY, 9).isoformat())
    store.add_excretion("urine", local(DAY - timedelta(days=1), 9).isoformat())
    sleep = SleepStore(store.database)
    start = local(DAY, 0)
    sleep.write_minutes(
        [
            MinuteRecord(start + timedelta(minutes=i), "asleep" if i < 600 else "awake", 60)
            for i in range(1440)
        ]
    )
    activity = ActivityStore(tmp_path)
    activity.add("a" * 32, DAY, 86400 * 0.9, (1, 1), (2, 2))
    activity.add("a" * 32, DAY, 0, None, (3, 3))
    days = gather(tmp_path, store, DAY - timedelta(days=1), DAY)
    today = days[DAY]
    assert today["feces"] == 2
    assert today["urine"] == 1
    assert today["sleep"] == 600
    # 通過2回を、観測できた90%の時間から1日分に直す。
    assert today["moves"] == pytest.approx(2 / 0.9)
    yesterday = days[DAY - timedelta(days=1)]
    assert yesterday["urine"] == 1
    assert yesterday["sleep"] is None
    assert yesterday["moves"] is None


def test_gather_without_activity_database_does_not_create_it(tmp_path):
    store = Store(tmp_path)
    days = gather(tmp_path, store, DAY, DAY)
    assert days[DAY]["moves"] is None
    assert not (tmp_path / "activity.sqlite3").exists()


def test_unusual_lists_flags_for_each_day():
    days = {
        day: {"moves": value, "sleep": None, "feces": 2, "urine": 3}
        for day, value in series([200] * 14 + [20]).items()
    }
    found = unusual(days, DAY)
    assert [item.metric for item in found] == ["moves"]


# --- 通知 ---


class Notifier:
    def __init__(self):
        self.sent = []

    def send_trend(self, labels, day):
        self.sent.append((labels, day))
        return True


def test_notices_send_once_when_two_days_in_a_row_are_unusual(tmp_path):
    store = Store(tmp_path)
    notices = TrendNotices(store.database)
    days = {
        day: {"moves": value, "sleep": None, "feces": 2, "urine": 3}
        for day, value in series([200] * 14 + [20, 20]).items()
    }
    notifier = Notifier()
    assert notices.notify(days, DAY + timedelta(days=1), notifier) == ["moves"]
    assert notifier.sent == [(["移動が少ない"], DAY + timedelta(days=1))]
    assert notices.notify(days, DAY + timedelta(days=1), notifier) == []
    assert len(notifier.sent) == 1


def test_notices_skip_a_single_unusual_day(tmp_path):
    store = Store(tmp_path)
    days = {
        day: {"moves": value, "sleep": None, "feces": 2, "urine": 3}
        for day, value in series([200] * 15 + [20]).items()
    }
    assert TrendNotices(store.database).notify(days, DAY + timedelta(days=1), Notifier()) == []


def test_failed_notification_is_retried(tmp_path):
    class Failing(Notifier):
        def send_trend(self, labels, day):
            super().send_trend(labels, day)
            return False

    store = Store(tmp_path)
    notices = TrendNotices(store.database)
    days = {
        day: {"moves": value, "sleep": None, "feces": 2, "urine": 3}
        for day, value in series([200] * 14 + [20, 20]).items()
    }
    failing = Failing()
    assert notices.notify(days, DAY + timedelta(days=1), failing) == []
    assert notices.notify(days, DAY + timedelta(days=1), Notifier()) == ["moves"]


# --- 文面・画面・定期の確認 ---


def test_trend_message_lists_labels_and_links_to_the_day():
    from poop_detector.notifications import ToiletNotifier

    class Messages(ToiletNotifier):
        def __init__(self):
            super().__init__(True, "ポチ")
            self.messages = []

        def _deliver(self, message, *, early_alert, path=""):
            self.messages.append((message, path))
            return True

    notifier = Messages()
    assert notifier.send_trend(["睡眠が少ない", "おしっこが多い"], DAY)
    message, path = notifier.messages[0]
    assert "ポチの様子が2日続けていつもと違います（睡眠が少ない、おしっこが多い）" in message
    assert path == "/trends?day=2026-11-20"


def add_daily_records(store, days, per_day):
    for day in days:
        for index in range(per_day(day)):
            store.add_excretion("urine", local(day, 8 + index).isoformat())


def test_trends_page_and_home_mark_an_unusual_yesterday(gui):
    from support import exchange, login

    from poop_detector.timezones import local_today

    server, store, _ = gui
    today = local_today()
    yesterday = today - timedelta(days=1)
    days = [yesterday - timedelta(days=i) for i in range(15)]
    add_daily_records(store, days, lambda day: 9 if day == yesterday else 3)
    cookie = login(server, role="member")
    _, _, body = exchange(server, "GET", "/", headers=cookie, auth=False)
    assert "昨日はいつもと違いました（おしっこが多い）" in body.decode()
    status, _, body = exchange(server, "GET", "/trends", headers=cookie, auth=False)
    html = body.decode()
    assert status == 200
    assert "9回" in html
    assert "多い" in html
    assert "（途中）" in html
    status, _, body = exchange(server, "GET", "/trends?day=bad", headers=cookie, auth=False)
    assert status == 200
    _, _, body = exchange(server, "GET", "/records", headers=cookie, auth=False)
    assert 'href="/trends?date=' in body.decode()


def test_trend_watch_notifies_through_the_gui_watch(tmp_path, monkeypatch):
    from poop_detector.commands import context as context_module
    from poop_detector.commands.context import CommandContext, trend_watch
    from poop_detector.config import Config
    from poop_detector.timezones import local_today

    store = Store(tmp_path)
    today = local_today()
    days = [today - timedelta(days=i) for i in range(1, 18)]
    add_daily_records(store, days, lambda day: 9 if day >= today - timedelta(days=2) else 3)
    checks = []

    def watching(check, interval):
        checks.append(interval)
        check()
        from contextlib import nullcontext

        return nullcontext()

    monkeypatch.setattr("poop_detector.features.watchdog.watching", watching)
    notifier = Notifier()
    context = CommandContext(Config(data_dir=str(tmp_path)))
    with trend_watch(context, notifier):
        pass
    assert checks == [context_module.TREND_CHECK_SECONDS]
    assert notifier.sent == [(["おしっこが多い"], today)]
    off = CommandContext(Config(data_dir=str(tmp_path), trends_notify=False))
    with trend_watch(off, notifier):
        pass
    assert len(notifier.sent) == 1
