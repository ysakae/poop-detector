"""シートの交換どき（#269）の、汚れの積み重なりの記録と通知を検証する。"""

from datetime import UTC, datetime

import cv2
import numpy as np
import pytest
from support import FrameSource, SilentNotifier, spot_scene

from poop_detector.config import Config, ConfigError
from poop_detector.detection.difference import IncrementalDetector, Observation
from poop_detector.detection.monitoring import FrameView, Monitor, MonitorExtension
from poop_detector.detection.roi import ROI
from poop_detector.detection.scene import scene_signature
from poop_detector.features.sheet_wear import SheetWearExtension
from poop_detector.notifications import ToiletNotifier
from poop_detector.records.care import CareEntry, save_entry
from poop_detector.records.database import events_database
from poop_detector.records.sheet_wear import SheetWear, SheetWearLog

EMPTY, STAIN = spot_scene()


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SheetNotifier:
    """交換どきの通知を覚える偽の通知先。"""

    def __init__(self, accept=True):
        self.accept = accept
        self.due = []

    def send(self, *, clip_id=None, detected_at=None):
        return False

    def send_sheet_due(self, events, ratio):
        self.due.append((events, ratio))
        return self.accept


class Messages(ToiletNotifier):
    def __init__(self):
        super().__init__(True, "ポチ")
        self.messages = []

    def _deliver(self, message, *, early_alert, path=""):
        self.messages.append((message, path))
        return True


def log(tmp_path):
    return SheetWearLog(events_database(tmp_path))


def view(frame=STAIN):
    return FrameView(frame, frame, frame, None, Observation("candidate", 0.1), 0.0)


def stained(count):
    """count 個の暗い四角を置いた画像（汚れが積み重なったシートの代わり）。"""
    frame = EMPTY.copy()
    for index in range(count):
        x = 10 + (index % 5) * 38
        y = 10 + (index // 5) * 38
        cv2.rectangle(frame, (x, y), (x + 25, y + 25), (30, 30, 30), -1)
    return frame


# --- 汚れの割合 ---


def test_wear_ratio_measures_the_stain_area_of_the_reference():
    detector = IncrementalDetector(EMPTY)
    assert detector.wear_ratio() == 0.0
    detector.accept(STAIN)
    # 21×21 の四角が 200×200 の画像に占める割合（ぼかしで少し変わる）。
    assert detector.wear_ratio() == pytest.approx(21 * 21 / 200**2, rel=0.3)


def test_wear_ratio_counts_only_the_valid_mask():
    detector = IncrementalDetector(EMPTY)
    detector.accept(STAIN)
    mask = np.zeros(EMPTY.shape[:2], np.uint8)
    mask[:, :100] = 255
    # 四角は左半分に収まるので、分母が半分になった分だけ割合が倍になる。
    assert detector.wear_ratio(mask) == pytest.approx(2 * detector.wear_ratio(), rel=0.1)


# --- 台帳 ---


def test_status_is_empty_before_any_record(tmp_path):
    status = log(tmp_path).status()
    assert status == SheetWear(since=None, events=0, ratio=0.0, notified=False)
    assert not status.measured


def test_status_counts_events_and_keeps_latest_ratio(tmp_path):
    sheet = log(tmp_path)
    sheet.record("scene", 0.05, "event")
    sheet.record("scene", 0.12, "event")
    sheet.record("scene", 0.10, "settled")
    status = sheet.status()
    assert (status.events, status.ratio, status.notified) == (2, 0.10, False)
    assert status.measured


def test_legacy_auto_reset_keeps_events_and_notification(tmp_path):
    sheet = log(tmp_path)
    sheet.record("scene", 0.2, "event")
    sheet.mark_notified()
    # 旧バージョンが残した自動リセットは割合の測定値を持たない。
    with sheet.database.connect() as db:
        db.execute(
            "INSERT INTO sheet_wear (at, scene_signature, kind, source) VALUES (?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(), "scene", "reset", "auto"),
        )
    status = sheet.status()
    assert (status.events, status.ratio, status.notified) == (1, 0.0, True)
    assert status.since is None


def test_changing_the_scene_keeps_events_and_uses_latest_ratio(tmp_path):
    sheet = log(tmp_path)
    sheet.record("old", 0.2, "event")
    sheet.record("new", 0.01, "event")
    status = sheet.status()
    assert (status.events, status.ratio) == (2, 0.01)


def test_sheet_change_in_care_records_resets(tmp_path):
    database = events_database(tmp_path)
    sheet = SheetWearLog(database)
    sheet.record("scene", 0.2, "event")
    later = datetime.now(UTC)
    save_entry(database, CareEntry("", later, "sheet", "家族", ""))
    status = sheet.status()
    assert (status.events, status.ratio) == (0, 0.0)
    assert status.since == later


def test_due_by_events_or_ratio_and_zero_disables():
    status = SheetWear(since=None, events=6, ratio=0.1, notified=False)
    assert status.due(6, 0.3)
    assert not status.due(7, 0.3)
    assert status.due(0, 0.1)
    assert not status.due(0, 0)


# --- 拡張 ---


def extension(tmp_path, notifier, **settings):
    config = Config(data_dir=str(tmp_path), **settings)
    sheet = log(tmp_path)
    return SheetWearExtension(sheet, "scene", config, notifier), sheet


def test_extension_notifies_once_when_due(tmp_path):
    notifier = SheetNotifier()
    ext, sheet = extension(tmp_path, notifier, sheet_max_events=2, sheet_max_ratio=0)
    detector = IncrementalDetector(EMPTY)
    for count in (1, 2, 3):
        detector.accept(stained(count))
        ext.on_reference(view(stained(count)), detector, "event")
    assert len(notifier.due) == 1
    assert notifier.due[0][0] == 2
    assert sheet.status().notified


def test_extension_retries_when_notification_fails(tmp_path):
    notifier = SheetNotifier(accept=False)
    ext, sheet = extension(tmp_path, notifier, sheet_max_events=1, sheet_max_ratio=0)
    detector = IncrementalDetector(EMPTY)
    detector.accept(STAIN)
    ext.on_reference(view(), detector, "event")
    ext.on_reference(view(), detector, "settled")
    assert len(notifier.due) == 2
    assert not sheet.status().notified


def test_settling_below_one_percent_preserves_event_count(tmp_path):
    notifier = SheetNotifier()
    ext, sheet = extension(tmp_path, notifier)
    detector = IncrementalDetector(EMPTY)
    detector.accept(STAIN)
    ext.on_reference(view(), detector, "event")
    faint_stain = EMPTY.copy()
    cv2.rectangle(faint_stain, (10, 10), (20, 20), (30, 30, 30), -1)
    detector.accept(faint_stain)
    ratio = detector.wear_ratio()
    assert 0 < ratio <= 0.01
    ext.on_reference(view(faint_stain), detector, "settled")
    status = sheet.status()
    assert (status.events, status.ratio) == (1, ratio)
    assert status.since is None


def test_disabled_setting_builds_no_extension(tmp_path):
    from poop_detector.features.monitoring import standard_extensions

    config = Config(data_dir=str(tmp_path), sheet_enabled=False)
    roi = ROI(0, 0, 1, 1)
    built = standard_extensions(roi, EMPTY, config, SheetNotifier(), Clock())
    assert not any(isinstance(item, SheetWearExtension) for item in built)
    enabled = standard_extensions(
        roi, EMPTY, Config(data_dir=str(tmp_path)), SheetNotifier(), Clock()
    )
    sheet = next(item for item in enabled if isinstance(item, SheetWearExtension))
    assert sheet.signature == scene_signature(EMPTY, roi)


# --- 監視ループの呼び出し口 ---


class References(MonitorExtension):
    def __init__(self):
        self.calls = []

    def on_reference(self, view, detector, reason):
        self.calls.append((reason, detector.reference_image is not None))


def test_monitor_tells_extensions_after_each_reference_update(tmp_path):
    recorder = References()
    config = Config(
        data_dir=str(tmp_path), fps=100, verify_seconds=0.01, clear_seconds=0.01, clip_enabled=False
    )
    camera = FrameSource(EMPTY, STAIN, STAIN, STAIN, STAIN, EMPTY, EMPTY, EMPTY, EMPTY)
    engine = Monitor(
        camera,
        ROI(0, 0, 1, 1),
        EMPTY,
        config,
        SilentNotifier(),
        extensions=[recorder],
        clock=Clock(),
    )
    engine.run(len(camera.frames))
    assert [reason for reason, _ in recorder.calls] == ["event", "settled"]


# --- 通知の文面と設定 ---


def test_sheet_due_message_mentions_count_and_ratio():
    notifier = Messages()
    assert notifier.send_sheet_due(6, 0.234)
    message, path = notifier.messages[0]
    assert "ポチのトイレ" in message
    assert "6回" in message
    assert "23%" in message
    assert path == "/"


def test_sheet_due_message_without_events():
    notifier = Messages()
    notifier.send_sheet_due(0, 0.4)
    assert "排泄" not in notifier.messages[0][0]


def test_sheet_settings_have_defaults_and_ranges():
    config = Config()
    assert (config.sheet_enabled, config.sheet_max_events, config.sheet_max_ratio) == (
        True,
        6,
        0.3,
    )
    with pytest.raises(ConfigError, match=r"sheet\.max_events"):
        Config(sheet_max_events=-1)
    with pytest.raises(ConfigError, match=r"sheet\.max_ratio"):
        Config(sheet_max_ratio=1.5)


# --- ホーム ---


def test_home_shows_sheet_wear_after_monitoring(gui):
    from support import exchange, login

    server, store, _ = gui
    cookie = login(server, role="member")
    _, _, body = exchange(server, "GET", "/", headers=cookie, auth=False)
    assert "シートの使用" not in body.decode()
    sheet = SheetWearLog(store.database)
    for _ in range(6):
        sheet.record("scene", 0.12, "event")
    _, _, body = exchange(server, "GET", "/", headers=cookie, auth=False)
    html = body.decode()
    assert "シートの使用" in html
    assert "排泄 6回" in html
    assert "汚れ 12%" in html
    assert "そろそろ交換どきです" in html
