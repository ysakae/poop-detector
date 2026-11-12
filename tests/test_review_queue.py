"""確認待ち（未ラベルの検知イベントと動画をまとめた列）の選び方を検証する。"""

import numpy as np

from poop_detector.gui.review_queue import next_item, pending_items
from poop_detector.records.events import Store
from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clips import ClipStore


def event(event_id, at, *, kind="SUSPECTED", feedback=None):
    return {"id": event_id, "kind": kind, "feedback": feedback, "timestamp": at}


def clip(clip_id, at, source="detected", **values):
    row = {
        "id": clip_id,
        "event_id": None,
        "source": source,
        "status": "ready",
        "label": None,
        "review_label": None,
        "requested_at": at,
    }
    return {**row, **values}


def keys(items):
    return [item.key for item in items]


def test_unlabeled_detections_wait_for_review_newest_first():
    events = [
        event("old", "2026-11-10T01:00:00+00:00"),
        event("new", "2026-11-11T01:00:00+00:00"),
        event("done", "2026-11-11T02:00:00+00:00", feedback=1),
        event("manual", "2026-11-11T03:00:00+00:00", kind="MANUAL"),
    ]
    assert keys(pending_items(events, [])) == ["event:new", "event:old"]


def test_only_excretion_related_clips_wait_for_review():
    clips = [
        clip("detected", "2026-11-11T01:00:00+00:00", "detected"),
        clip("manual", "2026-11-11T02:00:00+00:00", "manual"),
        clip("alert", "2026-11-11T03:00:00+00:00", "early_alert"),
        clip("outside", "2026-11-11T04:00:00+00:00", "outside_stay"),
        clip("enter", "2026-11-11T05:00:00+00:00", "dog_enter"),
        clip("flagged", "2026-11-11T06:00:00+00:00", "dog_exit", review_label="urine"),
        clip("calm", "2026-11-11T07:00:00+00:00", "dog_exit", review_label="none"),
        clip("labeled", "2026-11-11T08:00:00+00:00", "manual", label="none"),
        clip("encoding", "2026-11-11T09:00:00+00:00", "manual", status="encoding"),
        clip("removed", "2026-11-11T10:00:00+00:00", "manual", status="removed"),
    ]
    assert keys(pending_items([], clips)) == [
        "clip:flagged",
        "clip:outside",
        "clip:alert",
        "clip:manual",
        "clip:detected",
    ]


def test_detection_with_a_video_is_one_item_reviewed_on_the_video():
    events = [event("e1", "2026-11-11T01:00:00+00:00")]
    clips = [clip("c1", "2026-11-11T01:00:05+00:00", event_id="e1")]
    items = pending_items(events, clips)
    assert keys(items) == ["clip:c1"]
    assert items[0].event_id == "e1"


def test_labeling_either_side_finishes_the_combined_item():
    clips = [clip("c1", "2026-11-11T01:00:05+00:00", "dog_enter", event_id="e1")]
    labeled_event = [event("e1", "2026-11-11T01:00:00+00:00", feedback=0)]
    assert pending_items(labeled_event, clips) == []
    labeled_clip = [{**clips[0], "label": "uncertain"}]
    assert pending_items([event("e1", "2026-11-11T01:00:00+00:00")], labeled_clip) == []


def test_detection_falls_back_to_its_image_when_the_video_is_not_ready():
    events = [event("e1", "2026-11-11T01:00:00+00:00")]
    clips = [clip("c1", "2026-11-11T01:00:05+00:00", event_id="e1", status="failed")]
    assert keys(pending_items(events, clips)) == ["event:e1"]


def test_an_event_with_several_videos_appears_once():
    events = [event("e1", "2026-11-11T01:00:00+00:00")]
    clips = [
        clip("c2", "2026-11-11T01:01:00+00:00", "early_alert", event_id="e1"),
        clip("c1", "2026-11-11T01:00:05+00:00", event_id="e1"),
    ]
    assert len(pending_items(events, clips)) == 1


def test_next_item_follows_the_current_one_and_ends_after_the_last():
    items = pending_items(
        [event(name, f"2026-11-1{n}T00:00:00+00:00") for n, name in enumerate(["c", "b", "a"])], []
    )
    assert keys(items) == ["event:a", "event:b", "event:c"]
    assert next_item(items, None).key == "event:a"
    assert next_item(items, "event:a").key == "event:b"
    assert next_item(items, "event:c") is None
    # 保存して列から消えた項目の後は、その時刻より古い件へ進む。
    assert next_item(items, "event:gone", at="2026-11-11T12:00:00+00:00").key == "event:b"
    assert next_item(items, "event:gone", at="2026-11-10T00:00:00+00:00") is None
    assert next_item(items, "event:gone").key == "event:a"
    assert next_item([], None) is None


def test_review_urls_open_the_existing_editors():
    items = pending_items(
        [event("e1", "2026-11-11T01:00:00+00:00")], [clip("c1", "2026-11-11T02:00:00+00:00")]
    )
    assert [item.url for item in items] == [
        "/clips?id=c1&from=review",
        "/?filter=unlabeled&id=e1&from=review",
    ]


def test_no_excretion_on_the_video_marks_the_detection_as_false(tmp_path):
    store = Store(tmp_path)
    event_id = store.record(np.full((50, 50, 3), 150, np.uint8), 0.1, None, 1)
    clips = ClipStore(tmp_path)
    clip_id = clips.request(event_id=event_id)
    clips.update(clip_id, status="ready", path=f"clips/{clip_id}.mp4", duration=30)
    clips.labels.save(clip_id, ClipLabel("none", None, None, "other", "", None))
    row = next(row for row in store.list() if row["id"] == event_id)
    assert row["feedback"] == 0


def test_no_excretion_on_the_video_keeps_an_existing_detection_label(tmp_path):
    store = Store(tmp_path)
    event_id = store.record(np.full((50, 50, 3), 150, np.uint8), 0.1, None, 1)
    store.classify(event_id, "urine")
    clips = ClipStore(tmp_path)
    clip_id = clips.request(event_id=event_id)
    clips.update(clip_id, status="ready", path=f"clips/{clip_id}.mp4", duration=30)
    clips.labels.save(clip_id, ClipLabel("none", None, None, "other", "", None))
    row = next(row for row in store.list() if row["id"] == event_id)
    assert (row["feedback"], row["excretion_type"]) == (1, "urine")
