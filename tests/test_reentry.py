"""実カメラなしで排泄候補の追跡と再侵入の境界条件を確かめる。"""

import numpy as np
import pytest

from poop_detector.detection.roi import MaskedROI
from poop_detector.features.reentry import Presence, ReentryStore, dog_position
from poop_detector.records.events import Store


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        ("OOO", ["outside"]),
        ("OOII", ["outside", "inside"]),
        ("OOIIOOII", ["outside", "inside", "outside", "inside"]),
        ("IIOO", ["outside"]),
        ("OOI?II", ["outside", "inside"]),
        ("OOI?O", ["outside"]),
        ("OOI-I", ["outside"]),
    ],
)
def test_reentry_requires_a_sustained_exit_and_entry(samples, expected):
    presence = Presence(seconds=1, max_gap=2)
    changes = []
    for tick, sample in enumerate(samples):
        if sample == "-":
            presence.interrupt()
            continue
        change = presence.update({"O": "outside", "I": "inside", "?": None}[sample], tick)
        if change is not None:
            changes.append(change)
    assert changes == expected


def test_boundary_contact_does_not_count_as_entry():
    toilet = MaskedROI([[0.1, 0.2], [0.45, 0.2], [0.45, 0.8], [0.1, 0.8]])
    shape = (101, 101, 3)
    assert dog_position((36, 25, 57, 60), toilet, None, shape, 0.02)[0] is None
    assert dog_position((16, 25, 38, 60), toilet, None, shape, 0.02)[0] == "inside"
    assert dog_position((54, 25, 70, 60), toilet, None, shape, 0.02)[0] == "outside"
    assert dog_position((54, 25, 100, 60), toilet, None, shape, 0.02)[0] is None


def test_candidate_cleanup_and_notice_are_persistent_and_per_event(tmp_path):
    events = Store(tmp_path)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    first = events.record(frame, 0.1, None, 0)
    second = events.record(frame, 0.2, None, 0)
    risk = ReentryStore(tmp_path)
    assert risk.pending("before") == []
    risk.register(first, "before", None)
    risk.register(second, "before", (0.2, 0.4))
    assert risk.claim(first, "uncollected")
    assert not ReentryStore(tmp_path).claim(first, "uncollected")
    assert risk.claim(second, "uncollected")
    assert risk.collect(first)
    assert not risk.collect(first)
    remaining = risk.pending("before")
    assert [(row["id"], row["position_x"]) for row in remaining] == [(second, 0.2)]
    assert risk.pending("after") == []
    assert len(events.list()) == 2


def test_monitor_extension_keeps_visits_through_occlusion_and_records_repeat(tmp_path):
    from poop_detector.features.reentry import ReentryMonitor
    from poop_detector.records.events import utcnow

    events = Store(tmp_path)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    event_id = events.record(frame, 0.1, None, 0)
    store = ReentryStore(tmp_path)
    monitor = ReentryMonitor(store, "first", Presence(1, 2), 0.05)
    # 排泄前の往復は記録されない。
    for tick, side in enumerate(("outside", "outside", "inside", "inside")):
        assert monitor.observe(side, None, tick, utcnow()) == []
    store.register(event_id, "first", (0.2, 0.4))
    notices = []
    for tick, side in enumerate(
        (
            "inside",
            "outside",
            "outside",
            "inside",
            "inside",
            None,
            "inside",
            "outside",
            "outside",
            "inside",
            "inside",
        ),
        start=4,
    ):
        notices.extend(monitor.observe(side, (0.22, 0.4), tick, utcnow()))
    assert [(event, kind) for event, kind, _ in notices] == [
        (event_id, "reentry"),
        (event_id, "high"),
        (event_id, "reentry"),
        (event_id, "high"),
    ]
    monitor.interrupt()
    with store.db.connect() as db:
        visits = db.execute("SELECT * FROM reentry_visits ORDER BY id").fetchall()
    assert len(visits) == 2
    assert visits[0]["ended_at"] is not None
    assert visits[1]["interrupted"] == 1
    assert store.pending("first")[0]["position_x"] == 0.2


def test_local_profile_and_localized_difference():
    from poop_detector.features.reentry_extension import Layout, waste_position

    layout = Layout.from_data(
        {
            "configuration_id": "before",
            "installed_at": "2026-11-16T00:00:00+09:00",
            "description": "仕切りなし",
            "toilet_zone": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
            "living_zone": [[0, 0], [1, 0], [1, 0.1], [0, 0.1]],
        }
    )
    mask = np.zeros((81, 81), dtype=np.uint8)
    mask[38:43, 38:43] = 255
    assert waste_position(mask, layout.toilet, (101, 101, 3), 8) == pytest.approx((0.5, 0.5))
    mask[:] = 255
    assert waste_position(mask, layout.toilet, (101, 101, 3), 8) is None
    mask[:] = 0
    assert waste_position(mask, layout.toilet, (101, 101, 3), 8) is None


def test_report_uses_reviewed_feces_and_excludes_false_visits(tmp_path):
    from datetime import datetime

    from poop_detector.commands.reentry import report
    from poop_detector.records.events import utcnow

    events = Store(tmp_path)
    store = ReentryStore(tmp_path)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    first = events.record(frame, 0.1, None, 0)
    second = events.record(frame, 0.1, None, 0)
    events.classify(first, "feces", location="tray")
    for event_id in (first, second):
        store.register(event_id, "camera", None)
        visit = store.start(event_id, utcnow())
        assert visit is not None
        store.finish(visit, utcnow())
    events.classify(second, "false")
    assert store.start(second, utcnow()) is None
    assert not store.claim(second, "reentry")
    summary = report(store, datetime.fromisoformat(utcnow()).astimezone().tzinfo)["camera"]
    assert summary["confirmed_defecations"] == 1
    assert summary["risk_counts"] == {"MEDIUM": 1, "HIGH": 0}
    assert summary["first_reentry_seconds"][0] >= 0
    assert summary["small_sample"] is True


def test_cli_feedback_and_manual_collection(tmp_path, capsys):
    import json

    from poop_detector.main import main

    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {tmp_path}\n")
    events = Store(tmp_path)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    event_id = events.record(frame, 0.1, None, 0)
    store = ReentryStore(tmp_path)
    store.register(event_id, "first", None)
    visit = store.start(event_id, events.list()[0]["timestamp"])
    args = ["--config", str(config), "reentry"]
    assert main([*args, "event-feedback", event_id, "--label", "feces", "--stool", "soft"]) == 0
    assert main([*args, "visit-feedback", str(visit), "--correct", "yes", "--stepped", "yes"]) == 0
    assert main([*args, "collect", event_id]) == 0
    capsys.readouterr()
    assert main([*args, "list"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "collected"
    assert row["stool"] == "soft"
    assert row["visits"][0]["stepped"] == 1
    assert row["visits"][0]["interrupted"] == 1


def test_low_risk_requires_observed_outside_and_is_cleared_on_interruption(tmp_path):
    from poop_detector.features.reentry import ReentryMonitor
    from poop_detector.records.events import utcnow

    events = Store(tmp_path)
    event = events.record(np.zeros((48, 64, 3), dtype=np.uint8), 0.1, None, 0)
    store = ReentryStore(tmp_path)
    store.register(event, "first", None)
    monitor = ReentryMonitor(store, "first", Presence(1, 2), 0.05)
    monitor.observe("outside", None, 0, utcnow())
    assert store.pending("first")[0]["current_risk"] is None
    monitor.observe("outside", None, 1, utcnow())
    assert store.pending("first")[0]["current_risk"] == "LOW"
    monitor.interrupt()
    assert store.pending("first")[0]["current_risk"] is None


def test_monitor_factory_records_candidate_and_reentry_with_shared_inference(tmp_path):
    import json
    from dataclasses import replace

    import cv2

    from poop_detector.config import Config, check_monitor_features
    from poop_detector.detection.difference import IncrementalDetector
    from poop_detector.detection.monitoring import FrameView, SystemClock
    from poop_detector.features.dog import DogObserver
    from poop_detector.features.monitoring import standard_extensions
    from poop_detector.notifications import ToiletNotifier

    profile = {
        "configuration_id": "first",
        "installed_at": "2026-11-16T00:00:00+09:00",
        "description": "仕切りなし",
        "toilet_zone": [[0.1, 0.2], [0.5, 0.2], [0.5, 0.85], [0.1, 0.85]],
        "living_zone": [[0.5, 0.1], [0.99, 0.1], [0.99, 0.9], [0.5, 0.9]],
    }
    (tmp_path / "reentry-layout.json").write_text(json.dumps(profile))
    config = check_monitor_features(
        replace(
            Config(),
            data_dir=str(tmp_path),
            clip_enabled=True,
            reentry_enabled=True,
        )
    )
    assert config.dog_enabled
    roi = MaskedROI(profile["toilet_zone"])
    empty = np.full((101, 101, 3), 200, np.uint8)
    changed = empty.copy()
    cv2.rectangle(changed, (23, 55), (32, 64), (15, 15, 15), -1)
    baseline = roi.crop(empty)
    detector = IncrementalDetector(baseline)
    crop = roi.crop(changed)
    mask = roi.valid_mask(changed.shape)
    observation = detector.analyze(crop, mask)

    class Detector:
        def predict(self, _frame):
            return []

    class Collector:
        def __init__(self):
            self.saved = []

        def trigger(self, *, source):
            self.saved.append(source)
            return "recorded-clip"

    dog = DogObserver(Detector())
    collector = Collector()
    dog_extension, reentry = standard_extensions(
        roi,
        baseline,
        config,
        ToiletNotifier(enabled=False),
        SystemClock(),
        dog_observer=dog,
        collector=collector,
    )
    events = Store(tmp_path)
    view = FrameView(changed, changed, crop, mask, observation, 0)
    event_id = events.record(
        changed,
        observation.score,
        None,
        0,
        record_details=lambda db, event_id: reentry.on_event(db, event_id, view, detector),
    )
    for tick, box in enumerate([(61, 20, 80, 59)] * 3 + [(19, 25, 36, 59)] * 4):
        dog.detector.predict = lambda _frame, box=box: [box]
        dog_extension.observer.update = lambda frame, roi, _now, tick=tick: DogObserver.update(
            dog, frame, roi, tick
        )
        dog_extension.on_frame(view)
        reentry.on_frame(view)
    with events.connect() as db:
        visit = db.execute("SELECT * FROM reentry_visits WHERE event_id=?", (event_id,)).fetchone()
    assert collector.saved[-1] == "toilet_reentry"
    assert visit["risk"] == "HIGH"
    assert visit["high_clip_id"] == "recorded-clip"


def test_waste_tracking_requires_visible_matching_frames():
    from poop_detector.features.reentry import WasteTrack

    track = WasteTrack()
    origin = (0.2, 0.4)
    moved = (0.22, 0.4)
    assert track.update(moved, origin, 0.04) is None
    assert track.update(None, origin, 0.04) is None
    assert track.update(moved, origin, 0.04) is None
    assert track.update(moved, origin, 0.04) == moved
    assert track.update((0.8, 0.3), moved, 0.04) is None


def test_report_marks_unobserved_windows_unknown(tmp_path):
    from poop_detector.commands.reentry import report

    events = Store(tmp_path)
    store = ReentryStore(tmp_path)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    first = events.record(frame, 0.1, None, 0)
    second = events.record(frame, 0.1, None, 0)
    start = "2020-01-01T00:00:00+00:00"
    for event in (first, second):
        with events.connect() as db:
            db.execute("UPDATE events SET timestamp=? WHERE id=?", (start, event))
        events.classify(event, "feces")
        store.register(event, "layout", None)
    store.advance_observation(first, None, "2020-01-01T00:00:01+00:00")
    store.advance_observation(first, "2020-01-01T00:00:01+00:00", "2020-01-01T00:06:00+00:00")
    rates = report(store)["layout"]["reentry_rates"]
    assert rates[5] == {"count": 0, "denominator": 1, "unknown": 1, "rate": 0, "small_sample": True}
    assert rates[15] == {
        "count": 0,
        "denominator": 0,
        "unknown": 2,
        "rate": None,
        "small_sample": True,
    }


def test_notice_retries_only_explicit_failures_and_never_reclaims_a_sent_notice(tmp_path):
    store = ReentryStore(tmp_path)
    events = Store(tmp_path)
    event = events.record(np.zeros((48, 64, 3), np.uint8), 0.1, None, 0)
    store.register(event, "layout", None)
    first = "2026-01-01T00:00:00+00:00"
    due = "2026-01-01T00:01:00+00:00"
    assert store.claim(event, "high", first, "clip")
    assert store.due_notices("layout", due) == []
    store.finish_notice(event, "high", "retry", first)
    restarted = ReentryStore(tmp_path)
    assert restarted.due_notices("layout", first) == []
    retry = restarted.due_notices("layout", due)[0]
    assert retry["clip_id"] == "clip"
    assert retry["occurred_at"] == first
    assert restarted.claim_retry(event, "high", due)
    assert not store.claim_retry(event, "high", due)
    restarted.finish_notice(event, "high", "sent", due)
    assert not store.claim(event, "high")
    assert store.due_notices("layout", due) == []


def test_observation_gap_does_not_resume_a_negative_window(tmp_path):
    from poop_detector.features.reentry import ReentryMonitor

    events = Store(tmp_path)
    event = events.record(np.zeros((48, 64, 3), np.uint8), 0.1, None, 0)
    start = "2020-01-01T00:00:00+00:00"
    with events.connect() as db:
        db.execute("UPDATE events SET timestamp=? WHERE id=?", (start, event))
    store = ReentryStore(tmp_path)
    store.register(event, "layout", None)
    monitor = ReentryMonitor(store, "layout", Presence(1, 2), 0.04)
    for tick, side in enumerate(("outside", "outside", None, "outside", "outside"), 1):
        monitor.observe(side, None, tick, f"2020-01-01T00:00:0{tick}+00:00")
    with events.connect() as db:
        row = db.execute(
            "SELECT observed_until FROM reentry_waste WHERE event_id=?", (event,)
        ).fetchone()
    assert row["observed_until"] == "2020-01-01T00:00:02+00:00"


def test_partial_notice_delivery_prevents_retry():
    from poop_detector.notifications import merge_delivery

    assert merge_delivery(["retry", "skipped"]) == "retry"
    assert merge_delivery(["retry", "sent"]) == "sent"
    assert merge_delivery(["unknown", "retry"]) == "unknown"


@pytest.mark.parametrize(("code", "expected"), [(429, "retry"), (503, "retry"), (401, "unknown")])
def test_ntfy_retries_only_explicit_temporary_rejections(code, expected):
    from datetime import UTC, datetime
    from urllib.error import HTTPError

    from poop_detector.notifications import NtfyNotifier

    def reject(request, **_kwargs):
        raise HTTPError(request.full_url, code, "エラー", {}, None)

    notifier = NtfyNotifier(topic="example", opener=reject)
    assert notifier.send_reentry("high", "event", datetime.now(UTC), None) == expected


def _reentry_extension(tmp_path):
    """人工の配置で、犬検出を偽物にした再侵入の拡張を作る。"""
    import json
    from dataclasses import replace

    from poop_detector.config import Config, check_monitor_features
    from poop_detector.detection.monitoring import SystemClock
    from poop_detector.features.dog import DogObserver
    from poop_detector.features.monitoring import standard_extensions
    from poop_detector.notifications import ToiletNotifier

    profile = {
        "configuration_id": "first",
        "installed_at": "2026-11-16T00:00:00+09:00",
        "description": "仕切りなし",
        "toilet_zone": [[0.1, 0.2], [0.5, 0.2], [0.5, 0.85], [0.1, 0.85]],
        "living_zone": [[0.5, 0.1], [0.99, 0.1], [0.99, 0.9], [0.5, 0.9]],
    }
    (tmp_path / "reentry-layout.json").write_text(json.dumps(profile))
    config = check_monitor_features(
        replace(Config(), data_dir=str(tmp_path), clip_enabled=True, reentry_enabled=True)
    )
    roi = MaskedROI(profile["toilet_zone"])
    empty = np.full((101, 101, 3), 200, np.uint8)

    class Detector:
        def predict(self, _frame):
            return []

    class Collector:
        def trigger(self, *, source):
            return None

    _, reentry = standard_extensions(
        roi,
        roi.crop(empty),
        config,
        ToiletNotifier(enabled=False),
        SystemClock(),
        dog_observer=DogObserver(Detector()),
        collector=Collector(),
    )
    return reentry, roi, empty


def _view(roi, frame):
    from poop_detector.detection.difference import Observation
    from poop_detector.detection.monitoring import FrameView

    # 通知済み状態との比較は空、犬は生活エリアにいる前提で、位置の更新だけを確かめる。
    return FrameView(
        frame, frame, roi.crop(frame), roi.valid_mask(frame.shape), Observation("empty", 0.0), 0
    )


def _spot(empty, left, top=55, size=10, brighten=0):
    import cv2

    frame = np.clip(empty.astype(np.int16) + brighten, 0, 255).astype(np.uint8)
    cv2.rectangle(frame, (left, top), (left + size - 1, top + size - 1), (15, 15, 15), -1)
    return frame


@pytest.mark.parametrize(
    ("description", "start", "frames"),
    [
        # 照明が一斉に変わった映像は、基準画像との比較が unstable になる。
        ("照明変化", (0.275, 0.595), lambda empty: _spot(empty, 25, brighten=60)),
        # 監視範囲の縁にかかる差分は、犬の体や影による遮蔽として扱う。
        ("縁の遮蔽", (0.15, 0.595), lambda empty: _spot(empty, 10)),
        # 排泄物が見えなくなっても、片付けたとは判断しない。
        ("一時的な消失", (0.275, 0.595), lambda empty: empty.copy()),
    ],
)
def test_waste_position_and_status_are_kept_while_unobservable(
    tmp_path, description, start, frames
):
    reentry, roi, empty = _reentry_extension(tmp_path)
    event_id = Store(tmp_path).record(empty, 0.1, None, 0)
    reentry.store.register(event_id, "first", start)
    for _ in range(5):
        reentry.update_waste(_view(roi, frames(empty)))
    rows = reentry.store.pending("first")
    assert [(row["id"], row["position_x"], row["position_y"]) for row in rows] == [
        (event_id, *start)
    ], description
    with reentry.store.db.connect() as db:
        status = db.execute(
            "SELECT status FROM reentry_waste WHERE event_id=?", (event_id,)
        ).fetchone()
    assert status["status"] == "uncollected"


def test_waste_position_follows_a_small_visible_move(tmp_path):
    reentry, roi, empty = _reentry_extension(tmp_path)
    event_id = Store(tmp_path).record(empty, 0.1, None, 0)
    reentry.store.register(event_id, "first", (0.275, 0.595))
    moved = _view(roi, _spot(empty, 25))
    reentry.update_waste(moved)
    assert reentry.store.pending("first")[0]["position_x"] == 0.275
    reentry.update_waste(moved)
    assert reentry.store.pending("first")[0]["position_x"] == pytest.approx(0.295)


def test_temporary_loss_and_interruption_never_mark_waste_collected(tmp_path):
    from poop_detector.features.reentry import ReentryMonitor
    from poop_detector.records.events import utcnow

    event = Store(tmp_path).record(np.zeros((48, 64, 3), np.uint8), 0.1, None, 0)
    store = ReentryStore(tmp_path)
    store.register(event, "first", (0.2, 0.4))
    monitor = ReentryMonitor(store, "first", Presence(1, 2), 0.05)
    for tick, side in enumerate(("outside", "outside", "inside", "inside", None, None)):
        monitor.observe(side, None, tick, utcnow())
    monitor.observe(None, None, 20, utcnow())
    monitor.interrupt()
    assert [(row["id"], row["position_x"]) for row in store.pending("first")] == [(event, 0.2)]


@pytest.mark.parametrize(
    ("box", "expected"),
    [
        ((30, 30, 60, 70), "good"),
        # 画面端の近くでは、検出枠の下辺が切れている恐れがある。
        ((30, 30, 60, 98), "low"),
        ((1, 30, 40, 70), "low"),
        # 遠くて小さい検出枠は、下辺中央と足元のずれが相対的に大きい。
        ((40, 40, 48, 48), "low"),
    ],
)
def test_foot_quality_marks_uncertain_foot_points(box, expected):
    from poop_detector.features.reentry import foot_quality

    assert foot_quality(box, (101, 101, 3), 0.02) == expected


def test_visits_record_foot_quality_without_changing_risk(tmp_path):
    from poop_detector.features.reentry import ReentryMonitor
    from poop_detector.records.events import utcnow

    event = Store(tmp_path).record(np.zeros((48, 64, 3), np.uint8), 0.1, None, 0)
    store = ReentryStore(tmp_path)
    store.register(event, "first", (0.2, 0.4))
    monitor = ReentryMonitor(store, "first", Presence(1, 2), 0.05, high_samples=2)
    samples = [
        ("outside", None, "good"),
        ("outside", None, "good"),
        ("inside", (0.6, 0.6), "good"),
        ("inside", (0.6, 0.6), "low"),
        ("inside", (0.2, 0.41), "low"),
        ("inside", (0.2, 0.4), "good"),
    ]
    for tick, (side, point, quality) in enumerate(samples):
        monitor.observe(side, point, tick, utcnow(), quality=quality)
    with store.db.connect() as db:
        visit = db.execute(
            "SELECT risk,foot_samples,low_foot_samples,high_foot_quality FROM reentry_visits"
        ).fetchone()
    # 入場の確定後の3回を数える。品質が低い足元も、いまは HIGH の根拠に数える。
    assert tuple(visit) == ("HIGH", 3, 2, "low")


def test_visit_notices_share_a_clip_and_prefer_high_risk():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from poop_detector.features.reentry_extension import ReentryExtension

    store = Mock()
    notify = Mock()
    collector = Mock()
    collector.trigger.return_value = "clip-id"
    extension = SimpleNamespace(store=store, notify=notify, collector=collector)
    notices = [("first", "reentry", 1), ("first", "high", 1), ("second", "reentry", 2)]
    at = "2026-11-19T00:00:00+00:00"

    ReentryExtension._process_visit_notices(extension, notices, at)

    collector.trigger.assert_called_once_with(source="toilet_reentry")
    assert [call.args for call in notify.call_args_list] == [
        ("first", "high", at, "clip-id"),
        ("second", "reentry", at, "clip-id"),
    ]
    assert [(call.args, call.kwargs["high"]) for call in store.attach_clip.call_args_list] == [
        ((1, "clip-id"), False),
        ((1, "clip-id"), True),
        ((2, "clip-id"), False),
    ]


def test_event_feedback_rolls_back_classification_when_detail_cannot_be_saved(tmp_path):
    import sqlite3

    events = Store(tmp_path)
    event_id = events.record(np.zeros((8, 8, 3), np.uint8), 0.1, None, 0)
    store = ReentryStore(tmp_path)
    store.register(event_id, "first", None)
    with store.db.connect() as db:
        db.execute("""
            CREATE TRIGGER reject_feedback BEFORE UPDATE OF note ON reentry_waste
            BEGIN SELECT RAISE(ABORT, 'メモ保存失敗'); END
        """)

    with pytest.raises(sqlite3.IntegrityError, match="メモ保存失敗"):
        store.save_event_feedback(event_id, label="feces", stool="soft", note="確認済み")

    assert events.list()[0]["feedback"] is None
    with store.db.connect() as db:
        row = db.execute("SELECT stool, note FROM reentry_waste").fetchone()
        assert tuple(row) == (None, "")
