"""トレー外の録画、判定、通知、確定記録を検証する。"""

import json

import cv2
import numpy as np
from support import ready_clip, request, submit

from poop_detector.clips import ClipStore
from poop_detector.codex_review import inspect_clip
from poop_detector.dog import OutsideWatch
from poop_detector.roi import ROI


def test_outside_dwell_triggers_once_until_dog_moves():
    tray = ROI(0.4, 0.2, 0.4, 0.5)
    scope = ROI(0.1, 0.1, 0.8, 0.8)
    watch = OutsideWatch(tray, scope, exit_seconds=2)
    shape = (100, 100, 3)
    on_tray = [(48, 25, 65, 45)]
    outside = [(13, 25, 30, 45)]
    moved = [(13, 51, 30, 71)]
    for now in (0, 1, 2):
        assert not watch.update(on_tray, shape, now)
    assert not watch.update(outside, shape, 3)
    assert watch.update(outside, shape, 5)
    watch.recorded = True
    assert not watch.update(outside, shape, 7)
    assert not watch.update(moved, shape, 8)
    assert watch.update(moved, shape, 10)


def test_codex_classifies_location_with_outside_scope(tmp_path):
    path = tmp_path / "outside.mp4"
    frame = np.full((80, 100, 3), 80, np.uint8)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 1, (100, 80))
    assert writer.isOpened()
    try:
        for _ in range(6):
            writer.write(frame)
    finally:
        writer.release()

    def runner(command, *, cwd, **kwargs):
        schema = json.loads((cwd / "schema.json").read_text())
        assert "location" in schema["required"]
        assert any("トレー外" in part for part in command)
        (cwd / "result.json").write_text(
            json.dumps(
                {
                    "label": "urine",
                    "activity": "excreting",
                    "evidence": "new_wet_patch",
                    "location": "outside",
                    "start_seconds": None,
                    "end_seconds": 5,
                    "reason": "緑の枠の外に新しい濡れ跡があります",
                }
            )
        )

    result = inspect_clip(
        path,
        {"duration": 6},
        ROI(0.4, 0.2, 0.4, 0.5),
        runner=runner,
        outside_roi=ROI(0.1, 0.1, 0.8, 0.8),
    )
    assert (result.label, result.location) == ("urine", "outside")


def test_video_confirmation_records_outside_separately_from_tray_event(gui):
    server, store, tray_event = gui
    clips = ClipStore(store.root)
    clip = ready_clip(clips, event_id=tray_event)
    fields = {"id": clip, "label": "urine", "location": "outside", "record_id": tray_event}
    assert submit(server, "/clips/confirm", fields)[0] == 400
    fields["record_id"] = "new"
    assert submit(server, "/clips/confirm", fields)[0] == 303
    (record,) = store.records()
    assert record["id"] != tray_event
    assert record["location"] == "outside"
    assert clips.get(clip)["location"] == "outside"
    _, page = request(server, "GET", "/records")
    assert "トレー外".encode() in page


def test_outside_notice_is_distinct_and_does_not_mark_tray_event(gui):
    from datetime import UTC, datetime, timedelta

    from poop_detector.codex_review import Assessment, Review

    _, store, tray_event = gui
    clips = ClipStore(store.root)
    now = datetime.now(UTC)
    locations = {}
    for offset, location in enumerate(("outside", "outside", "tray")):
        clip = clips.request(
            source="outside_stay" if location == "outside" else "detected",
            event_id=tray_event if offset == 0 else None,
        )
        stamp = (now + timedelta(seconds=offset)).isoformat()
        path = clips.root / "clips" / f"{clip}.mp4"
        path.touch()
        clips.update(
            clip,
            status="ready",
            path=f"clips/{clip}.mp4",
            start_at=stamp,
            duration=12,
            scene_signature="test",
        )
        with store.connect() as db:
            db.execute("UPDATE clips SET requested_at=? WHERE id=?", (stamp, clip))
        locations[clip] = location

    notices = []
    review = Review(
        clips,
        lambda path, row: Assessment(
            "urine", "excreting", 4, 6, "新しい濡れ跡", locations[row["id"]]
        ),
        lambda label, location=None: notices.append((label, location)) or True,
    )
    assert review.run(limit=3, since=now.isoformat()) == (3, 0)
    assert notices == [("urine", "outside"), ("urine", None)]
    (event,) = store.list()
    assert event["notified_at"] is None


def test_monitor_queues_outside_stay_without_alerting_on_the_dog_alone(tmp_path, monkeypatch):
    from poop_detector import main
    from poop_detector.config import Config
    from poop_detector.dog import DogState

    now = 0
    frame = np.full((100, 100, 3), 80, np.uint8)
    tray = ROI(0.4, 0.2, 0.4, 0.5)
    scope = ROI(0.1, 0.1, 0.8, 0.8)

    class Camera:
        generation = 1

        def read(self):
            nonlocal now
            now += 1
            return frame

    class Dog:
        def update(self, *_):
            return DogState(True, False, None, [(13, 25, 30, 45)], 1)

    class Collector:
        def __init__(self):
            self.sources = []

        def poll(self):
            pass

        def trigger(self, *, source):
            self.sources.append(source)
            return "recording"

    class Notifier:
        def send(self):
            raise AssertionError("犬が立ち止まっただけで通知しない")

    monkeypatch.setattr(main.time, "monotonic", lambda: now)
    monkeypatch.setattr(main.time, "sleep", lambda _: None)
    collector = Collector()
    assert (
        main.monitor(
            Camera(),
            tray,
            tray.crop(frame),
            Config(data_dir=str(tmp_path)),
            Notifier(),
            max_frames=5,
            collector=collector,
            dog_observer=Dog(),
            codex_notifications=True,
            outside_roi=scope,
        )
        == 5
    )
    assert collector.sources == ["outside_stay"]
