"""床に落ちた物の候補の検知（#270、実験的）を検証する。"""

import cv2
import numpy as np
import pytest

from poop_detector.config import Config, check_monitor_features
from poop_detector.detection.monitoring import FrameView
from poop_detector.detection.roi import ROI
from poop_detector.experimental.floor_objects import (
    DOG_GRACE_SECONDS,
    PERSIST_SECONDS,
    SAMPLE_SECONDS,
    FloorObjectExtension,
    FloorObjectWatch,
)
from poop_detector.notifications import ToiletNotifier
from poop_detector.records.database import events_database
from poop_detector.records.floor_objects import FLOOR_LABELS, FloorObjects

FLOOR = ROI(0.0, 0.0, 1.0, 1.0)
TRAY = ROI(0.7, 0.7, 0.3, 0.3)


def floor(width=320, height=240):
    """少し模様のある床（明るさの一様な画像だと差分の検査が甘くなる）。"""
    image = np.full((height, width, 3), 170, np.uint8)
    for x in range(0, width, 40):
        cv2.line(image, (x, 0), (x, height), (150, 150, 150), 1)
    return image


def with_object(image, box=(100, 80, 115, 95), color=(40, 60, 90)):
    changed = image.copy()
    x1, y1, x2, y2 = box
    cv2.rectangle(changed, (x1, y1), (x2, y2), color, -1)
    return changed


def run(watch, frames, start=0.0, step=SAMPLE_SECONDS, boxes=()):
    """frames を step 秒ごとに渡し、見つかった候補を時刻とともに返す。"""
    found = []
    now = start
    for frame in frames:
        for candidate in watch.update(frame, list(boxes), now):
            found.append((now, candidate))
        now += step
    return found, now


def samples(seconds):
    return int(seconds / SAMPLE_SECONDS) + 1


# --- 候補の見つけ方 ---


def test_new_object_that_stays_is_reported_once():
    watch = FloorObjectWatch(FLOOR, TRAY)
    empty = floor()
    found, now = run(watch, [empty] * 3)
    assert found == []
    found, _ = run(watch, [with_object(empty)] * (samples(PERSIST_SECONDS) + 5), start=now)
    assert len(found) == 1
    when, candidate = found[0]
    assert when - now >= PERSIST_SECONDS
    x1, y1, x2, y2 = candidate.box
    assert (x1, y1) == pytest.approx((100, 80), abs=4)
    assert (x2, y2) == pytest.approx((116, 96), abs=4)
    assert 0 < candidate.area_ratio < 0.01


def test_object_that_disappears_before_persisting_is_not_reported():
    watch = FloorObjectWatch(FLOOR, TRAY)
    empty = floor()
    _, now = run(watch, [empty] * 2)
    frames = [with_object(empty)] * 3 + [empty] * samples(PERSIST_SECONDS)
    found, _ = run(watch, frames, start=now)
    assert found == []


def test_removing_a_reported_object_is_not_reported_again():
    watch = FloorObjectWatch(FLOOR, TRAY)
    empty, dirty = floor(), with_object(floor())
    _, now = run(watch, [empty] * 2)
    found, now = run(watch, [dirty] * (samples(PERSIST_SECONDS) + 2), start=now)
    assert len(found) == 1
    found, _ = run(watch, [empty] * (samples(PERSIST_SECONDS) + 2), start=now)
    assert found == []


def test_objects_on_the_tray_or_outside_the_floor_are_ignored():
    floor_area = ROI(0.0, 0.0, 0.6, 1.0)
    watch = FloorObjectWatch(floor_area, TRAY)
    empty = floor()
    _, now = run(watch, [empty] * 2)
    # トレーの上と、床面の外（右側）に置いた物。
    dirty = with_object(with_object(empty, (260, 200, 275, 215)), (220, 20, 235, 35))
    found, _ = run(watch, [dirty] * (samples(PERSIST_SECONDS) + 2), start=now)
    assert found == []


def test_area_where_the_dog_was_is_ignored_for_a_while():
    watch = FloorObjectWatch(FLOOR, TRAY)
    empty = floor()
    _, now = run(watch, [empty] * 2)
    dog = [(90, 70, 130, 110)]
    # 犬が去った直後に残った物は、しばらく数えない（犬そのものの写り残りかもしれない）。
    _, now = run(watch, [with_object(empty)] * 2, start=now, boxes=dog)
    wait = samples(PERSIST_SECONDS + DOG_GRACE_SECONDS) + 2
    found, _ = run(watch, [with_object(empty)] * wait, start=now)
    first = found[0][0] if found else None
    assert first is not None
    # 犬が去ってからの猶予を過ぎてから、続いた時間を数え始める。
    assert first - now >= PERSIST_SECONDS + DOG_GRACE_SECONDS - SAMPLE_SECONDS


def test_large_changes_such_as_lighting_relearn_the_background():
    watch = FloorObjectWatch(FLOOR, TRAY)
    empty = floor()
    _, now = run(watch, [empty] * 2)
    darker = (empty * 0.5).astype(np.uint8)
    found, _ = run(watch, [darker] * (samples(PERSIST_SECONDS) + 2), start=now)
    assert found == []


def test_dog_sized_blob_is_not_a_floor_object():
    watch = FloorObjectWatch(FLOOR, TRAY)
    empty = floor()
    _, now = run(watch, [empty] * 2)
    big = with_object(empty, (40, 40, 140, 140))
    found, _ = run(watch, [big] * (samples(PERSIST_SECONDS) + 2), start=now)
    assert found == []


def test_samples_are_spaced_and_reset_forgets_tracks():
    watch = FloorObjectWatch(FLOOR, TRAY)
    empty = floor()
    assert watch.update(empty, [], 0.0) == []
    _, now = run(watch, [empty] * 2, start=SAMPLE_SECONDS)
    dirty = with_object(empty)
    _, now = run(watch, [dirty] * (samples(PERSIST_SECONDS) - 2), start=now)
    watch.reset()
    # 中断の後は数え直すので、すぐには知らせない。
    found, _ = run(watch, [dirty] * 3, start=now)
    assert found == []


# --- 台帳 ---


def test_store_saves_image_lists_and_labels(tmp_path):
    objects = FloorObjects(events_database(tmp_path), tmp_path)
    frame = with_object(floor())
    object_id = objects.add(frame, (100, 80, 116, 96), 0.003)
    assert objects.image_path(object_id).is_file()
    assert objects.pending_count() == 1
    [row] = objects.list()
    assert row.id == object_id
    assert row.label is None
    assert row.box == pytest.approx((100 / 320, 80 / 240, 116 / 320, 96 / 240))
    objects.label(object_id, "vomit", "家族")
    assert objects.pending_count() == 0
    assert objects.list()[0].label == "vomit"
    with pytest.raises(ValueError, match="unknown-label"):
        objects.label(object_id, "unknown-label", "家族")
    with pytest.raises(KeyError):
        objects.label("missing", "vomit", "家族")
    assert set(FLOOR_LABELS) >= {"foreign", "vomit", "diarrhea", "toy", "false"}


def test_image_path_rejects_unsafe_ids(tmp_path):
    objects = FloorObjects(events_database(tmp_path), tmp_path)
    with pytest.raises(KeyError):
        objects.image_path("../events")


# --- 拡張 ---


class Notifier:
    def __init__(self):
        self.sent = []

    def send(self, *, clip_id=None, detected_at=None):
        return False

    def send_floor_object(self, object_id, detected_at):
        self.sent.append(object_id)
        return True


class Predict:
    def __init__(self, boxes=()):
        self.boxes = list(boxes)

    def __call__(self, frame):
        return list(self.boxes)


def test_extension_records_and_notifies(tmp_path):
    objects = FloorObjects(events_database(tmp_path), tmp_path)
    notifier = Notifier()
    ext = FloorObjectExtension(
        FloorObjectWatch(FLOOR, TRAY), Predict(), objects, notifier, notify=True
    )
    empty = floor()
    now = 0.0
    for frame in [empty] * 2 + [with_object(empty)] * (samples(PERSIST_SECONDS) + 2):
        ext.on_camera_frame(frame, False, now)
        now += SAMPLE_SECONDS
    [row] = objects.list()
    assert notifier.sent == [row.id]
    assert row.notified


def test_extension_without_notification_only_records(tmp_path):
    objects = FloorObjects(events_database(tmp_path), tmp_path)
    notifier = Notifier()
    ext = FloorObjectExtension(
        FloorObjectWatch(FLOOR, TRAY), Predict(), objects, notifier, notify=False
    )
    empty = floor()
    now = 0.0
    for frame in [empty] * 2 + [with_object(empty)] * (samples(PERSIST_SECONDS) + 2):
        ext.on_camera_frame(frame, False, now)
        now += SAMPLE_SECONDS
    assert len(objects.list()) == 1
    assert notifier.sent == []


def test_extension_interrupts_on_disconnect_and_survives_detector_errors(tmp_path):
    objects = FloorObjects(events_database(tmp_path), tmp_path)

    def broken(_frame):
        raise cv2.error("推論の失敗")

    ext = FloorObjectExtension(FloorObjectWatch(FLOOR, TRAY), broken, objects, Notifier())
    ext.on_camera_frame(floor(), False, 0.0)
    ext.on_camera_frame(None, True, 1.0)
    ext.on_frame(FrameView(floor(), floor(), floor(), None, None, 0.0))  # 何もしない
    assert objects.list() == []


# --- 通知と設定 ---


class Messages(ToiletNotifier):
    def __init__(self):
        super().__init__(True, "ポチ")
        self.messages = []

    def _deliver(self, message, *, early_alert, path=""):
        self.messages.append((message, path))
        return True


def test_floor_object_message_links_to_the_page():
    notifier = Messages()
    assert notifier.send_floor_object("abc123", "2026-11-22T01:02:03+00:00")
    message, path = notifier.messages[0]
    assert "床" in message
    assert path == "/floor?id=abc123"


def test_floor_objects_need_dog_detection():
    config = check_monitor_features(Config(floor_objects_enabled=True))
    assert config.dog_enabled
    assert Config().floor_objects_enabled is False
    assert Config().floor_notify is True


# --- 画面 ---


def test_floor_page_lists_shows_image_and_saves_label(gui):
    from support import exchange, login, submit

    server, store, _ = gui
    objects = FloorObjects(store.database, server.app.root)
    cookie = login(server, role="member")
    _, _, body = exchange(server, "GET", "/", headers=cookie, auth=False)
    assert "床の候補" not in body.decode()
    object_id = objects.add(with_object(floor()), (100, 80, 116, 96), 0.003)
    _, _, body = exchange(server, "GET", "/", headers=cookie, auth=False)
    assert "床の候補 1件が未確認です" in body.decode()
    status, _, body = exchange(server, "GET", "/floor", headers=cookie, auth=False)
    html = body.decode()
    assert status == 200
    assert f"/floor-image?id={object_id}" in html
    assert "嘔吐" in html
    status, headers, image = exchange(
        server, "GET", f"/floor-image?id={object_id}", headers=cookie, auth=False
    )
    assert status == 200
    assert image[:2] == b"\xff\xd8"
    assert "max-age" in headers["cache-control"]
    status, _, _ = exchange(server, "GET", "/floor-image?id=../x", headers=cookie, auth=False)
    assert status == 404
    status, _ = submit(server, "/floor/label", {"id": object_id, "label": "vomit"}, page="/floor")
    assert status == 303
    assert objects.list()[0].label == "vomit"
    status, _ = submit(server, "/floor/label", {"id": object_id, "label": "nope"}, page="/floor")
    assert status == 400


def test_standard_extensions_add_floor_watch_sharing_dog_inference(tmp_path):
    from poop_detector.features.monitoring import standard_extensions

    calls = []

    class Observer:
        def predict(self, frame):
            calls.append(1)
            return []

    config = Config(data_dir=str(tmp_path), sheet_enabled=False)
    built = standard_extensions(
        TRAY, floor(), config, Notifier(), None, dog_observer=Observer(), floor=FLOOR
    )
    ext = next(item for item in built if isinstance(item, FloorObjectExtension))
    assert ext.notify is True
    ext.on_camera_frame(floor(), False, 0.0)
    assert calls == [1]
    with pytest.raises(ValueError, match="犬の検出"):
        standard_extensions(TRAY, floor(), config, Notifier(), None, floor=FLOOR)


def test_backup_includes_floor_object_images(tmp_path):
    import zipfile

    from poop_detector.records.backup import create_backup

    root = tmp_path / "data"
    objects = FloorObjects(events_database(root), root)
    object_id = objects.add(with_object(floor()), (100, 80, 116, 96), 0.003)
    create_backup(root, tmp_path / "backup.zip", include_clips=False)
    with zipfile.ZipFile(tmp_path / "backup.zip") as archive:
        assert f"floor-objects/{object_id}.jpg" in archive.namelist()
