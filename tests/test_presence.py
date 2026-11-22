"""サークルからの脱走と不在（散歩など）の記録と通知（#272）を検証する。"""

from datetime import datetime, timedelta

import pytest

from poop_detector.config import Config, ConfigError, check_monitor_features
from poop_detector.features.presence import PresenceExtension, PresenceTracker
from poop_detector.notifications import ToiletNotifier
from poop_detector.records.absences import ABSENCE_LABELS, Absences, AwayMode
from poop_detector.records.database import events_database

START = datetime(2026, 11, 22, 9, 0).astimezone()


class Notifier:
    def __init__(self, accept=True):
        self.accept = accept
        self.absent = []
        self.returned = []

    def send_absence(self, since, minutes):
        self.absent.append((since, minutes))
        return self.accept

    def send_return(self, minutes):
        self.returned.append(minutes)
        return self.accept


def tracker(tmp_path, notifier=None, **settings):
    database = events_database(tmp_path)
    config = Config(data_dir=str(tmp_path), **settings)
    absences, away = Absences(database), AwayMode(database)
    return PresenceTracker(absences, away, config, notifier or Notifier()), absences, away


def feed(track, observations, start=START, step=60):
    """observations を step 秒ごとに渡し、最後の時刻の次を返す。"""
    now = start
    for presence in observations:
        track.update(presence, now)
        now += timedelta(seconds=step)
    return now


# --- 不在の区間 ---


def test_short_absence_is_not_recorded(tmp_path):
    track, absences, _ = tracker(tmp_path, presence_min_minutes=3)
    feed(track, ["still", "left", "left", "moving"])
    assert absences.list() == []


def test_long_absence_is_recorded_from_the_first_left(tmp_path):
    track, absences, _ = tracker(tmp_path, presence_min_minutes=3)
    feed(track, ["still"] + ["left"] * 5 + ["moving"])
    [row] = absences.list()
    assert row.started_at == START + timedelta(minutes=1)
    assert row.ended_at == START + timedelta(minutes=6)
    assert row.minutes == pytest.approx(5)
    assert row.label is None
    assert not row.away


def test_unknown_keeps_the_absence_but_does_not_start_one(tmp_path):
    track, absences, _ = tracker(tmp_path, presence_min_minutes=3)
    feed(track, ["unknown"] * 10)
    assert absences.list() == []
    feed(track, ["left", "left", "unknown", "unknown", "left", "still"])
    [row] = absences.list()
    assert row.minutes == pytest.approx(5)


def test_open_absence_is_listed_and_closed_on_interrupt(tmp_path):
    track, absences, _ = tracker(tmp_path, presence_min_minutes=3)
    now = feed(track, ["left"] * 5)
    [row] = absences.list()
    assert row.ended_at is None
    track.interrupt(now)
    [row] = absences.list()
    # 映像が途切れた時刻ではなく、最後に「出ていった」を確かめた時刻で閉じる。
    assert row.ended_at == START + timedelta(minutes=4)
    assert row.interrupted


# --- 留守中の通知 ---


def test_absence_while_away_notifies_once_and_on_return(tmp_path):
    notifier = Notifier()
    track, absences, away = tracker(
        tmp_path, notifier, presence_min_minutes=3, presence_away_minutes=10
    )
    away.set_away(True)
    feed(track, ["left"] * 15 + ["still"])
    assert len(notifier.absent) == 1
    since, minutes = notifier.absent[0]
    assert since == START
    assert minutes == pytest.approx(10)
    assert notifier.returned == [pytest.approx(15)]
    [row] = absences.list()
    assert row.away
    assert row.notified


def test_absence_at_home_is_not_notified(tmp_path):
    notifier = Notifier()
    track, absences, _ = tracker(tmp_path, notifier, presence_away_minutes=10)
    feed(track, ["left"] * 15 + ["still"])
    assert notifier.absent == []
    assert notifier.returned == []
    assert len(absences.list()) == 1


def test_failed_absence_notification_is_retried_and_return_not_sent(tmp_path):
    notifier = Notifier(accept=False)
    track, absences, away = tracker(tmp_path, notifier, presence_away_minutes=10)
    away.set_away(True)
    feed(track, ["left"] * 12 + ["still"])
    assert len(notifier.absent) == 2
    assert notifier.returned == []
    assert not absences.list()[0].notified


# --- 台帳 ---


def test_away_mode_and_labels(tmp_path):
    database = events_database(tmp_path)
    away, absences = AwayMode(database), Absences(database)
    assert away.since() is None
    away.set_away(True)
    assert away.since() is not None
    away.set_away(False)
    assert away.since() is None
    identifier = absences.start(START, away=False)
    absences.end(identifier, START + timedelta(minutes=20))
    absences.label(identifier, "walk", "家族")
    assert absences.list()[0].label == "walk"
    with pytest.raises(ValueError, match="nope"):
        absences.label(identifier, "nope", "家族")
    with pytest.raises(KeyError):
        absences.label("missing", "walk", "家族")
    assert set(ABSENCE_LABELS) == {"walk", "out", "escape", "false"}


# --- 拡張 ---


class Sampler:
    def __init__(self, observations):
        self.observations = list(observations)
        self.resets = 0

    def sample(self, frame, boxes):
        return self.observations.pop(0), False

    def reset(self):
        self.resets += 1


class Recorder:
    def __init__(self):
        self.calls = []

    def update(self, presence, now):
        self.calls.append(presence)

    def interrupt(self, now):
        self.calls.append("interrupt")


def test_extension_samples_once_a_second_and_survives_errors():
    import cv2
    import numpy as np

    frame = np.zeros((10, 10, 3), np.uint8)
    sampler, recorder = Sampler(["still", "left"]), Recorder()
    ext = PresenceExtension(recorder, sampler, lambda f: [], clock=lambda: START)
    ext.on_camera_frame(frame, False, 0.0)
    ext.on_camera_frame(frame, False, 0.5)
    ext.on_camera_frame(frame, False, 1.0)
    assert recorder.calls == ["still", "left"]

    def broken(_frame):
        raise cv2.error("推論の失敗")

    ext = PresenceExtension(recorder, Sampler([]), broken, clock=lambda: START)
    ext.on_camera_frame(frame, False, 5.0)
    assert recorder.calls[-1] == "unknown"
    ext.on_camera_frame(None, True, 6.0)
    assert recorder.calls[-1] == "interrupt"


# --- 文面と設定 ---


class Messages(ToiletNotifier):
    def __init__(self):
        super().__init__(True, "ポチ")
        self.messages = []

    def _deliver(self, message, *, early_alert, path=""):
        self.messages.append((message, path))
        return True


def test_absence_messages():
    notifier = Messages()
    assert notifier.send_absence(START, 12)
    assert notifier.send_return(25)
    absent, back = notifier.messages
    assert "ポチ" in absent[0]
    assert "12分" in absent[0]
    assert absent[1] == "/presence"
    assert "25分" in back[0]


def test_presence_settings_need_dog_detection():
    assert Config().presence_enabled is False
    assert check_monitor_features(Config(presence_enabled=True)).dog_enabled
    with pytest.raises(ConfigError, match=r"presence\.away_minutes"):
        Config(presence_away_minutes=0)


# --- 画面 ---


def test_home_toggle_presence_page_and_labels(gui):
    from support import exchange, login, submit

    server, store, _ = gui
    database = store.database
    cookie = login(server, role="member")
    _, _, body = exchange(server, "GET", "/", headers=cookie, auth=False)
    assert "外出中にする" in body.decode()
    status, _ = submit(server, "/away", {"away": "1", "back": "/presence"}, page="/")
    assert status == 303
    assert AwayMode(database).since() is not None
    _, _, body = exchange(server, "GET", "/", headers=cookie, auth=False)
    assert "在宅に戻す" in body.decode()
    status, _ = submit(server, "/away", {"away": "maybe"}, page="/")
    assert status == 400
    absences = Absences(database)
    open_id = absences.start(datetime.now().astimezone() - timedelta(minutes=5), away=True)
    _, _, body = exchange(server, "GET", "/", headers=cookie, auth=False)
    assert "からサークルに姿が見えません" in body.decode()
    absences.end(open_id, datetime.now().astimezone())
    status, _, body = exchange(server, "GET", "/presence", headers=cookie, auth=False)
    html = body.decode()
    assert status == 200
    assert "約5分" in html
    assert "散歩" in html
    status, _ = submit(
        server, "/presence/label", {"id": open_id, "label": "walk"}, page="/presence"
    )
    assert status == 303
    assert absences.list()[0].label == "walk"
    status, _ = submit(server, "/presence/label", {"id": open_id, "label": "x"}, page="/presence")
    assert status == 400
    _, _, body = exchange(server, "GET", "/records", headers=cookie, auth=False)
    assert 'href="/presence"' in body.decode()
    status, _ = submit(server, "/away", {"away": "0", "back": "https://evil.example"}, page="/")
    assert status == 303
    assert AwayMode(database).since() is None


def test_standard_extensions_add_presence_sharing_dog_inference(tmp_path):
    import numpy as np

    from poop_detector.detection.roi import ROI
    from poop_detector.features.monitoring import standard_extensions

    calls = []

    class Observer:
        def predict(self, frame):
            calls.append(1)
            return []

    config = Config(data_dir=str(tmp_path), sheet_enabled=False, presence_enabled=True)
    image = np.full((60, 80, 3), 120, np.uint8)
    built = standard_extensions(
        ROI(0.7, 0.7, 0.3, 0.3),
        image,
        config,
        Notifier(),
        None,
        dog_observer=Observer(),
        floor=ROI(0, 0, 1, 1),
    )
    ext = next(item for item in built if isinstance(item, PresenceExtension))
    ext.on_camera_frame(image, False, 0.0)
    assert calls == [1]
