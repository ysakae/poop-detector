"""トレー外の録画、判定、通知、確定記録を検証する。"""

import json
from types import SimpleNamespace

import cv2
import numpy as np
from support import ready_clip, request, submit

from poop_detector.detection.roi import ROI
from poop_detector.experimental.outside import OutsideWatch
from poop_detector.video.clips import ClipStore
from poop_detector.video.codex_cli import CodexCliJudge
from poop_detector.video.review import inspect_clip


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
        judge=CodexCliJudge(runner),
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
    _, page = request(server, "GET", "/records?record_end=2026-11-09")
    assert "トレー外".encode() in page


def test_outside_notice_is_distinct_and_does_not_mark_tray_event(gui):
    from datetime import UTC, datetime, timedelta

    from poop_detector.video.review import Assessment, Review

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
        lambda path, row, judge: Assessment(
            "urine", "excreting", 4, 6, "新しい濡れ跡", locations[row["id"]]
        ),
        lambda label, location=None, **kwargs: notices.append((label, location)) or True,
    )
    assert review.run(limit=3, since=now.isoformat()) == (3, 0)
    # トレー内の検知を、トレー外の滞在より先に判定する。
    assert notices == [("urine", None), ("urine", "outside")]
    (event,) = store.list()
    assert event["notified_at"] is None


def test_monitor_queues_outside_stay_without_alerting_on_the_dog_alone(tmp_path, monkeypatch):
    from poop_detector.config import Config
    from poop_detector.detection import monitoring
    from poop_detector.features.dog import DogState
    from poop_detector.features.monitoring import monitor

    now = 0
    frame = np.full((100, 100, 3), 80, np.uint8)
    tray = ROI(0.4, 0.2, 0.4, 0.5)
    scope = ROI(0.1, 0.1, 0.8, 0.8)

    class Camera:
        generation = 1
        healthy = True

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
        def send(self, *, clip_id=None, detected_at=None):
            raise AssertionError("犬が立ち止まっただけで通知しない")

    monkeypatch.setattr(monitoring.time, "monotonic", lambda: now)
    monkeypatch.setattr(monitoring.time, "sleep", lambda _: None)
    collector = Collector()
    assert (
        monitor(
            Camera(),
            tray,
            tray.crop(frame),
            Config(data_dir=str(tmp_path)),
            Notifier(),
            max_frames=5,
            collector=collector,
            dog_observer=Dog(),
            review_notifications=True,
            outside_roi=scope,
        )
        == 5
    )
    assert collector.sources == ["outside_stay"]


class StayingDog:
    """犬がトレーの外にいて、位置を変えるたびに新しい滞在になる状況を作る。"""

    def __init__(self):
        self.box = (13, 25, 30, 45)

    def update(self, *_):
        from poop_detector.features.dog import DogState

        boxes = [] if self.box is None else [self.box]
        return DogState(bool(boxes), False, None, boxes, 1)

    def reset(self):
        pass


class OutsideClips:
    def __init__(self):
        self.triggers = []
        self.accept = True
        self.now = 0.0

    def trigger(self, *, source):
        if not self.accept:
            return None
        self.triggers.append((source, self.now))
        return "recording"


def outside_extension(tmp_path):
    from poop_detector.config import Config
    from poop_detector.experimental.outside import OutsideWatch
    from poop_detector.features.monitoring import DogExtension

    tray = ROI(0.4, 0.2, 0.4, 0.5)
    scope = ROI(0.1, 0.1, 0.8, 0.8)
    dog, clips = StayingDog(), OutsideClips()
    clock = SimpleNamespace(monotonic=lambda: clips.now)
    config = Config(data_dir=str(tmp_path), clip_before_seconds=30, clip_after_seconds=30)
    extension = DogExtension(dog, tray, config, clock, clips, OutsideWatch(tray, scope, 2))
    frame = np.full((100, 100, 3), 80, np.uint8)

    def advance(until, *, move_at=()):
        while clips.now < until:
            clips.now += 1
            if clips.now in move_at:
                # 体の大きさの4割より大きく動くと、別の場所での滞在として数え直す。
                dog.box = (60, 60, 77, 80) if dog.box[0] == 13 else (13, 25, 30, 45)
            extension.on_frame(SimpleNamespace(frame=frame, observation=SimpleNamespace(kind="")))

    return extension, clips, advance, dog


def test_outside_stays_in_quick_succession_are_recorded_without_overlap(tmp_path):
    _extension, clips, advance, _dog = outside_extension(tmp_path)
    # 3秒で最初の録画。10秒・20秒・40秒に場所を変えても、前の録画（前後30秒）と重ねない。
    advance(100, move_at=(10, 20, 40))
    assert clips.triggers == [("outside_stay", 3), ("outside_stay", 63)]


def test_a_single_outside_stay_is_recorded_once(tmp_path):
    _extension, clips, advance, _dog = outside_extension(tmp_path)
    advance(200)
    assert clips.triggers == [("outside_stay", 3)]


def test_outside_stay_after_the_previous_clip_is_recorded_at_once(tmp_path):
    _extension, clips, advance, _dog = outside_extension(tmp_path)
    advance(200, move_at=(100,))
    assert clips.triggers == [("outside_stay", 3), ("outside_stay", 102)]


def test_postponed_outside_clip_is_retried_when_the_recorder_is_busy_and_dropped_on_interrupt(
    tmp_path,
):
    extension, clips, advance, dog = outside_extension(tmp_path)
    advance(20, move_at=(10,))
    clips.accept = False
    advance(70)
    clips.accept = True
    advance(72)
    assert clips.triggers == [("outside_stay", 3), ("outside_stay", 71)]

    advance(80, move_at=(75,))
    # 映像が途切れたら、先に延ばした録画は捨てる。犬がいなくなれば、もう録画しない。
    extension.on_interrupt()
    dog.box = None
    advance(200)
    assert len(clips.triggers) == 2
