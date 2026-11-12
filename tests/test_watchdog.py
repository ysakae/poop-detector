"""監視の停止とカメラの不調の見張り（#91）を検証する。

時刻は偽の値を渡し、通知は記録するだけの偽物に差し替える。
"""

import sqlite3
from datetime import UTC, datetime

import numpy as np
import pytest

from poop_detector.capture.live_preview import LivePreview
from poop_detector.features.outages import OutageLog, OutageTracker, format_duration
from poop_detector.features.watchdog import CameraWatchdogExtension, MonitorWatchdog, watching
from poop_detector.notifications import FanOutNotifier, ToiletNotifier
from poop_detector.records.database import MIGRATIONS, events_database

FRAME = np.zeros((4, 4, 3), np.uint8)
# 2026-11-12 09:00:00 UTC
T0 = 1_794_474_000.0


class Alerts:
    """送った見張りの通知を記録する。result で送信の成否を決める。"""

    def __init__(self, result=True):
        self.result = result
        self.sent = []

    def send_outage(self, kind, since):
        self.sent.append(("outage", kind, since))
        return self.result

    def send_recovery(self, kind, seconds):
        self.sent.append(("recovery", kind, seconds))
        return self.result


@pytest.fixture
def log(tmp_path):
    return OutageLog(events_database(tmp_path))


def iso(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def test_outage_is_reported_once_after_the_threshold_and_recovery_with_duration(log):
    alerts = Alerts()
    tracker = OutageTracker(log, "camera", 300, alerts)
    tracker.observe(T0, T0 + 299)
    assert alerts.sent == []
    assert log.recent() == []
    tracker.observe(T0, T0 + 300)
    tracker.observe(T0, T0 + 900)
    assert alerts.sent == [("outage", "camera", datetime.fromtimestamp(T0, UTC))]
    (outage,) = log.recent()
    assert (outage.kind, outage.started_at, outage.ended_at) == ("camera", iso(T0), None)
    assert outage.notified_at == iso(T0 + 300)

    tracker.observe(None, T0 + 1200)
    assert alerts.sent[-1] == ("recovery", "camera", 1200)
    (outage,) = log.recent()
    assert outage.ended_at == iso(T0 + 1200)
    assert outage.seconds() == 1200
    # 復旧後の正常な観測では、何も送らない。
    tracker.observe(None, T0 + 1300)
    assert len(alerts.sent) == 2


def test_short_outages_are_neither_recorded_nor_reported(log):
    alerts = Alerts()
    tracker = OutageTracker(log, "camera", 300, alerts)
    tracker.observe(T0, T0 + 10)
    tracker.observe(None, T0 + 11)
    assert alerts.sent == []
    assert log.recent() == []


def test_unsent_alert_is_retried_and_recovery_needs_a_sent_alert(log):
    alerts = Alerts(result=False)
    tracker = OutageTracker(log, "camera", 60, alerts)
    tracker.observe(T0, T0 + 60)
    tracker.observe(T0, T0 + 61)
    assert len(alerts.sent) == 1
    assert log.recent()[0].notified_at is None
    # 再送の間隔が過ぎたら、もう一度送る。
    alerts.result = True
    tracker.observe(T0, T0 + 60 + OutageTracker.RETRY_SECONDS)
    assert len(alerts.sent) == 2
    assert log.recent()[0].notified_at is not None

    quiet = Alerts(result=False)
    other = OutageTracker(log, "monitor", 60, quiet)
    other.observe(T0, T0 + 60)
    other.observe(None, T0 + 70)
    # 停止を知らせていなければ、復旧も知らせない。
    assert [kind for kind, *_ in quiet.sent] == ["outage"]


def test_open_outage_is_taken_over_without_a_second_alert(log):
    """GUI を開き直したり、2つの見張りが同じデータを見ていたりしても、同じ停止で繰り返さない。"""
    first, second = Alerts(), Alerts()
    OutageTracker(log, "monitor", 60, first).observe(T0, T0 + 60)
    again = OutageTracker(log, "monitor", 60, second)
    again.observe(T0, T0 + 600)
    assert second.sent == []
    again.observe(None, T0 + 700)
    assert second.sent == [("recovery", "monitor", 700)]
    assert len(log.recent()) == 1


def test_end_is_reported_only_by_the_one_who_closes_it(log):
    first, second = Alerts(), Alerts()
    a = OutageTracker(log, "monitor", 60, first)
    b = OutageTracker(log, "monitor", 60, second)
    a.observe(T0, T0 + 60)
    b.observe(T0, T0 + 61)
    a.observe(None, T0 + 100)
    b.observe(None, T0 + 101)
    assert [kind for kind, *_ in first.sent] == ["outage", "recovery"]
    assert second.sent == []


def test_recent_lists_the_newest_first_and_close_open(log):
    for n in range(3):
        outage, created = log.begin("camera", iso(T0 + n * 100))
        assert created
        log.end(outage.id, iso(T0 + n * 100 + 10))
    log.begin("camera", iso(T0 + 1000))
    assert [o.started_at for o in log.recent(limit=2)] == [iso(T0 + 1000), iso(T0 + 200)]
    log.close_open("camera", iso(T0 + 2000))
    assert log.current("camera") is None
    assert log.recent()[0].ended_at == iso(T0 + 2000)


def test_unknown_kind_is_rejected(log):
    with pytest.raises(ValueError, match="種類"):
        log.begin("disk", iso(T0))


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (30, "1分未満"),
        (60, "約1分"),
        (59 * 60 + 59, "約59分"),
        (2 * 3600, "約2時間"),
        (2 * 3600 + 5 * 60, "約2時間5分"),
        (3 * 86400 + 4 * 3600 + 30, "約3日4時間"),
    ],
)
def test_duration_wording(seconds, text):
    assert format_duration(seconds) == text


class Wall:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_camera_watchdog_counts_time_without_frames(log):
    alerts = Alerts()
    wall = Wall(T0)
    watch = CameraWatchdogExtension(OutageTracker(log, "camera", 60, alerts), wall=wall)
    watch.on_camera_frame(FRAME, False, 0)
    for tick in range(1, 61):
        wall.now = T0 + tick
        watch.on_camera_frame(None, tick > 30, tick)
    assert [kind for kind, *_ in alerts.sent] == ["outage"]
    assert alerts.sent[0][2] == datetime.fromtimestamp(T0, UTC)
    wall.now = T0 + 90
    watch.on_camera_frame(FRAME, True, 90)
    assert alerts.sent[-1] == ("recovery", "camera", 90)


def test_camera_watchdog_ignores_paused_loops(log):
    """トレー交換の保留中は拡張が呼ばれない。再開した直後に不調と数えない。"""
    alerts = Alerts()
    wall = Wall(T0)
    watch = CameraWatchdogExtension(OutageTracker(log, "camera", 60, alerts), wall=wall)
    watch.on_camera_frame(FRAME, False, 0)
    wall.now = T0 + 600
    watch.on_camera_frame(None, False, 600)
    wall.now = T0 + 601
    watch.on_camera_frame(None, False, 601)
    assert alerts.sent == []


def test_camera_watchdog_closes_outages_left_by_the_previous_run(log):
    log.begin("camera", iso(T0))
    alerts = Alerts()
    CameraWatchdogExtension(OutageTracker(log, "camera", 60, alerts), wall=Wall(T0 + 5000))
    assert log.current("camera") is None
    assert log.recent()[0].ended_at == iso(T0 + 5000)
    assert alerts.sent == []


def test_monitor_heartbeat_and_clean_stop(tmp_path):
    preview = LivePreview(tmp_path)
    assert preview.monitor_heartbeat() is None
    preview.pulse(True)
    state, stamp = preview.monitor_heartbeat()
    assert state == "connected"
    assert abs(stamp - preview.monitor_path.stat().st_mtime) < 1
    preview.mark_stopped()
    assert preview.monitor_heartbeat()[0] == "stopped"
    assert preview.monitor_status() == "offline"


def write_heartbeat(preview, state, stamp):
    import os

    preview.monitor_path.write_text(state, encoding="ascii")
    os.utime(preview.monitor_path, (stamp, stamp))


def test_gui_watchdog_reports_a_stale_heartbeat(tmp_path, log):
    preview = LivePreview(tmp_path)
    alerts = Alerts()
    wall = Wall(T0 + 10)
    watchdog = MonitorWatchdog(preview, OutageTracker(log, "monitor", 300, alerts), wall=wall)
    # 監視を一度も動かしていなければ知らせない。
    watchdog.check()
    write_heartbeat(preview, "connected", T0)
    watchdog.check()
    wall.now = T0 + 300
    watchdog.check()
    assert alerts.sent == [("outage", "monitor", datetime.fromtimestamp(T0, UTC))]
    write_heartbeat(preview, "searching", T0 + 400)
    wall.now = T0 + 401
    watchdog.check()
    assert alerts.sent[-1] == ("recovery", "monitor", 401)


def test_gui_watchdog_stays_quiet_after_a_clean_stop(tmp_path, log):
    preview = LivePreview(tmp_path)
    alerts = Alerts()
    watchdog = MonitorWatchdog(
        preview, OutageTracker(log, "monitor", 60, alerts), wall=Wall(T0 + 3600)
    )
    write_heartbeat(preview, "stopped", T0)
    watchdog.check()
    assert alerts.sent == []
    assert log.recent() == []


def test_watching_runs_checks_until_stopped():
    import threading

    calls = threading.Event()
    failures = []

    def check():
        failures.append(1)
        if len(failures) == 1:
            raise sqlite3.OperationalError("locked")
        calls.set()

    with watching(check, interval=0.01):
        assert calls.wait(2)
    # 確認の失敗では止まらず、次の間隔でやり直す。
    assert len(failures) >= 2


class Recording(ToiletNotifier):
    def __init__(self, enabled=True, pet_name=""):
        super().__init__(enabled, pet_name)
        self.messages = []

    def _deliver(self, message, *, early_alert):
        self.messages.append(message)
        return True


def test_watchdog_messages_have_no_secrets_and_name_the_time():
    notifier = Recording(pet_name="ポチ")
    since = datetime(2026, 11, 12, 0, 30, tzinfo=UTC)
    assert notifier.send_outage("monitor", since)
    assert notifier.send_outage("camera", since)
    assert notifier.send_recovery("monitor", 3900)
    assert notifier.send_recovery("camera", 30)
    # テストは日本時間に固定している（conftest.py）。
    assert notifier.messages == [
        "ポチのトイレの監視が11/12 09:30から止まっています。監視するマシンを確認してください。",
        "11/12 09:30からカメラの映像が届いていません。カメラの電源とネットワークを確認してください。",
        "ポチのトイレの監視が再開しました。約1時間5分止まっていました。",
        "カメラの映像が戻りました。1分未満届いていませんでした。",
    ]
    assert not Recording(enabled=False).send_outage("camera", since)


def test_fan_out_sends_watchdog_alerts_through_every_channel():
    first, second = Recording(), Recording()
    assert FanOutNotifier([first, second]).send_recovery("camera", 120)
    assert (
        first.messages
        == second.messages
        == ["カメラの映像が戻りました。約2分届いていませんでした。"]
    )


def test_v9_database_gains_outages_and_admin_watchdog_preference(tmp_path):
    from poop_detector.gui.accounts import Accounts

    with sqlite3.connect(tmp_path / "events.sqlite3") as db:
        for migration in MIGRATIONS[:9]:
            migration(db)
        db.execute("PRAGMA user_version = 9")
        db.execute(
            "INSERT INTO users (id, name, role, created_at) VALUES ('old', '家族A', 'admin', '2026')"
        )
        db.execute("INSERT INTO events (id, timestamp) VALUES ('e1', '2026')")
    database = events_database(tmp_path)
    assert Accounts(database).users.get("old").push_watchdog is True
    assert OutageLog(database).recent() == []
    with database.connect() as db:
        assert db.execute("SELECT id FROM events").fetchone()["id"] == "e1"
