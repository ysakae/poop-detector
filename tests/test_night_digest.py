"""夜ごとの「夜間のまとめ」（#296）を検証する。"""

import os
from datetime import date, datetime, time, timedelta

import numpy as np
import pytest

from poop_detector.config import Config, ConfigError
from poop_detector.features.digest import DigestSnapshots, SnapshotExtension
from poop_detector.features.night import (
    NightNotices,
    night_id,
    night_of,
    night_window,
    parse_hours,
    sleep_summary,
)
from poop_detector.features.sleep_store import MinuteRecord, SleepStore
from poop_detector.notifications import ToiletNotifier
from poop_detector.records.absences import AwayMode
from poop_detector.records.away_periods import AwayPeriods
from poop_detector.records.database import events_database
from poop_detector.records.events import Store

FRAME = np.full((120, 160, 3), 128, np.uint8)
NIGHT = date(2026, 11, 22)
HOURS = "22:00-07:00"


def local(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute)).astimezone()


# --- 夜の時間帯 ---


def test_parse_hours_allows_crossing_midnight():
    assert parse_hours("22:00-07:00") == (time(22), time(7))
    assert parse_hours("01:00-05:30") == (time(1), time(5, 30))
    with pytest.raises(ValueError, match="HH:MM-HH:MM"):
        parse_hours("22-7")
    with pytest.raises(ValueError, match="同じ"):
        parse_hours("07:00-07:00")


def test_night_window_and_which_night():
    start, end = night_window(NIGHT, HOURS)
    assert (start, end) == (local(NIGHT, 22), local(NIGHT + timedelta(days=1), 7))
    assert night_of(local(NIGHT, 23), HOURS) == NIGHT
    assert night_of(local(NIGHT + timedelta(days=1), 3), HOURS) == NIGHT
    assert night_of(local(NIGHT, 12), HOURS) is None
    # 日をまたがない時間帯は、その日の夜。
    assert night_window(NIGHT, "01:00-05:00")[0] == local(NIGHT, 1)
    assert night_id(NIGHT) == "night-20261122"


# --- 睡眠の数字 ---


def minutes(start, states):
    return [MinuteRecord(start + timedelta(minutes=i), s, 60) for i, s in enumerate(states)]


def test_sleep_summary_counts_awakenings_and_longest_sleep():
    start = local(NIGHT, 22)
    states = (
        ["awake"] * 30
        + ["asleep"] * 120
        + ["awake"] * 10
        + ["asleep"] * 200
        + ["awake"] * 1
        + ["asleep"] * 60
    )
    summary = sleep_summary(minutes(start, states))
    assert summary.asleep_minutes == 380
    assert summary.observed_minutes == len(states)
    # 1分だけの身じろぎは起きたと数えない（AWAKE_MINUTES 以上で数える）。
    assert summary.awakenings == 1
    assert summary.longest_minutes == 261
    assert summary.awake_spans == [(start + timedelta(minutes=150), start + timedelta(minutes=160))]


def test_sleep_summary_without_records():
    summary = sleep_summary([])
    assert (summary.asleep_minutes, summary.awakenings, summary.longest_minutes) == (0, 0, 0)
    assert summary.observed_minutes == 0


# --- 静止画 ---


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_snapshots_are_saved_at_night_and_shared_with_away(tmp_path):
    database = events_database(tmp_path)
    periods, away = AwayPeriods(database), AwayMode(database)
    snapshots = DigestSnapshots(tmp_path)
    clock = Clock(local(NIGHT, 12))
    ext = SnapshotExtension(periods, snapshots, 60, clock=clock, night_hours=HOURS)
    ext.on_camera_frame(FRAME, False, 0.0)  # 昼で在宅なら残さない
    assert not snapshots.frames(night_id(NIGHT))
    clock.now = local(NIGHT, 23)
    ext.on_camera_frame(FRAME, False, 60.0)
    assert len(snapshots.frames(night_id(NIGHT))) == 1
    away.set_away(True)
    clock.now = local(NIGHT, 23, 5)
    ext.on_camera_frame(FRAME, False, 120.0)
    [night_frame, shared] = snapshots.frames(night_id(NIGHT))[-1:] + snapshots.frames(
        periods.current().id
    )
    # 外出中と重なったときは1回だけ書き、もう一方は同じファイルを指す。
    assert os.path.samefile(night_frame, shared)


def test_night_snapshots_are_off_without_hours(tmp_path):
    database = events_database(tmp_path)
    snapshots = DigestSnapshots(tmp_path)
    ext = SnapshotExtension(AwayPeriods(database), snapshots, 60, clock=Clock(local(NIGHT, 23)))
    ext.on_camera_frame(FRAME, False, 0.0)
    assert not snapshots.frames(night_id(NIGHT))


# --- 朝の知らせ ---


class Notifier:
    def __init__(self, accept=True):
        self.accept = accept
        self.sent = []

    def send_night_summary(self, night, asleep_minutes, awakenings):
        self.sent.append((night, asleep_minutes, awakenings))
        return self.accept


def sleep_night(store, night, asleep=300):
    start = local(night, 22)
    SleepStore(store.database).write_minutes(
        minutes(start, ["asleep"] * asleep + ["awake"] * 30 + ["asleep"] * 60)
    )


def test_morning_notice_is_sent_once_after_the_night(tmp_path):
    store = Store(tmp_path)
    sleep_night(store, NIGHT)
    notices = NightNotices(store.database)
    notifier = Notifier()
    morning = local(NIGHT + timedelta(days=1), 8)
    assert notices.notify(store, HOURS, morning, notifier) == NIGHT
    assert notifier.sent == [(NIGHT, 360, 1)]
    assert notices.notify(store, HOURS, morning, notifier) is None
    assert notices.notify(store, HOURS, local(NIGHT, 23), Notifier()) is None


def test_morning_notice_retries_and_skips_nights_without_records(tmp_path):
    store = Store(tmp_path)
    notices = NightNotices(store.database)
    morning = local(NIGHT + timedelta(days=1), 8)
    assert notices.notify(store, HOURS, morning, Notifier()) is None  # 記録のない夜
    sleep_night(store, NIGHT)
    failing = Notifier(accept=False)
    assert notices.notify(store, HOURS, morning, failing) is None
    assert notices.notify(store, HOURS, morning, Notifier()) == NIGHT


# --- 文面と設定 ---


class Messages(ToiletNotifier):
    def __init__(self):
        super().__init__(True, "ポチ")
        self.messages = []

    def _deliver(self, message, *, early_alert, path=""):
        self.messages.append((message, path))
        return True


def test_night_summary_message():
    notifier = Messages()
    assert notifier.send_night_summary(NIGHT, 390, 2)
    message, path = notifier.messages[0]
    assert "昨夜" in message
    assert "6時間30分" in message
    assert "2回" in message
    assert path == "/night?day=2026-11-22"


def test_night_settings():
    config = Config()
    assert (config.night_enabled, config.night_hours, config.night_notify) == (False, HOURS, False)
    with pytest.raises(ConfigError, match=r"night\.hours"):
        Config(night_hours="25:00-07:00")


# --- 画面 ---


def test_night_pages_show_numbers_timeline_and_home_link(gui, monkeypatch):
    from support import exchange, login

    from poop_detector.gui import night_routes

    server, store, _ = gui
    monkeypatch.setattr(night_routes, "night_config", lambda app: Config(night_enabled=True))
    today = datetime.now().astimezone().date()
    night = today - timedelta(days=1)
    sleep_night(store, night)
    store.add_excretion("urine", local(night, 23, 30).isoformat())
    snapshots = DigestSnapshots(server.app.root)
    for minute in range(3):
        snapshots.save(night_id(night), FRAME, local(night, 23) + timedelta(minutes=minute))
    cookie = login(server, role="member")
    status, _, body = exchange(server, "GET", "/night", headers=cookie, auth=False)
    assert status == 200
    assert f"/night?day={night.isoformat()}" in body.decode()
    status, _, body = exchange(
        server, "GET", f"/night?day={night.isoformat()}", headers=cookie, auth=False
    )
    html = body.decode()
    assert status == 200
    assert "6時間00分" in html  # 寝ていた時間
    assert "起きた回数" in html
    assert "排尿の記録" in html
    assert "起きていた（30分）" in html
    assert f"/digest-video?id={night_id(night)}" in html
    status, _, _ = exchange(server, "GET", "/night?day=bad", headers=cookie, auth=False)
    assert status == 404
    _, _, body = exchange(server, "GET", "/", headers=cookie, auth=False)
    assert f'href="/night?day={night.isoformat()}"' in body.decode()


def test_night_watch_runs_only_when_enabled_and_notify(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from poop_detector.commands.context import CommandContext, night_watch

    checks = []

    def watching(check, interval):
        checks.append(interval)
        check()
        return nullcontext()

    monkeypatch.setattr("poop_detector.features.watchdog.watching", watching)
    notifier = Notifier()
    for settings in ({}, {"night_enabled": True}):
        with night_watch(CommandContext(Config(data_dir=str(tmp_path), **settings)), notifier):
            pass
    assert checks == []
    on = CommandContext(Config(data_dir=str(tmp_path), night_enabled=True, night_notify=True))
    with night_watch(on, notifier):
        pass
    assert len(checks) == 1


def test_night_timelapse_folder_is_accepted(tmp_path):
    snapshots = DigestSnapshots(tmp_path)
    assert snapshots.folder(night_id(NIGHT)).name == "night-20261122"
    with pytest.raises(KeyError):
        snapshots.folder("night-2026112")
