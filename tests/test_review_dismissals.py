"""確認待ちから外した動画（review_dismissals）の台帳と、確認待ち・整理への反映を検証する。"""

from datetime import UTC, datetime, timedelta

from poop_detector.gui.review_queue import pending_items, scene_groups
from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.clip_storage import cleanup_candidates
from poop_detector.video.clips import ClipStore


def ready(clips, source="manual", *, days_ago=10, scene="old", event_id=None):
    clip_id = clips.request(source=source, event_id=event_id)
    relative = f"clips/{clip_id}.mp4"
    (clips.root / relative).write_bytes(b"video")
    clips.update(clip_id, status="ready", path=relative, duration=1, scene_signature=scene)
    requested = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with clips.store.connect() as db:
        db.execute("UPDATE clips SET requested_at=? WHERE id=?", (requested, clip_id))
    return clip_id


def waiting(clips):
    return [item.id for item in pending_items([], clips.list(include_assessments=True))]


def test_dismissed_clips_leave_the_queue_until_restored(tmp_path):
    clips = ClipStore(tmp_path)
    first = ready(clips)
    second = ready(clips, days_ago=9)
    assert clips.dismissals.dismiss([first]) == 1
    assert waiting(clips) == [second]
    # 同じ動画をもう一度外しても、数に入れない。
    assert clips.dismissals.dismiss([first]) == 0
    assert clips.dismissals.restore([first]) == 1
    assert sorted(waiting(clips)) == sorted([first, second])


def test_only_ready_unlabeled_clips_can_be_dismissed(tmp_path):
    clips = ClipStore(tmp_path)
    labeled = ready(clips)
    clips.labels.save(labeled, ClipLabel("none"))
    queued = clips.request()
    assert clips.dismissals.dismiss([labeled, queued, "missing"]) == 0
    assert all(not row["review_dismissed"] for row in clips.list(include_assessments=True))


def test_detection_with_a_dismissed_clip_does_not_come_back_as_an_image():
    events = [{"id": "e1", "kind": "SUSPECTED", "feedback": None, "timestamp": "2026-11-10"}]
    clip = {
        "id": "c1",
        "event_id": "e1",
        "source": "detected",
        "status": "ready",
        "label": None,
        "review_label": None,
        "requested_at": "2026-11-10",
        "review_dismissed": 1,
    }
    assert pending_items(events, [clip]) == []


def test_scene_groups_count_waiting_and_dismissed_clips_per_scene(tmp_path):
    clips = ClipStore(tmp_path)
    old = [ready(clips, days_ago=days, scene="old") for days in (12, 11)]
    dismissed = ready(clips, days_ago=10, scene="old")
    current = ready(clips, days_ago=1, scene="new")
    clips.dismissals.dismiss([dismissed])
    rows = clips.list(include_assessments=True)

    groups = scene_groups(pending_items([], rows), rows)

    assert [(g.scene, g.waiting, g.dismissed, g.current) for g in groups] == [
        ("new", 1, 0, True),
        ("old", 2, 1, False),
    ]
    assert groups[0].clip_ids == (current,)
    assert set(groups[1].clip_ids) == set(old)
    assert groups[1].thumbnail == old[1]


def test_dismissed_excretion_candidates_become_cleanup_candidates(tmp_path):
    clips = ClipStore(tmp_path)
    flagged = ready(clips, "dog_exit")
    kept = ready(clips, "dog_exit")
    for clip_id in (flagged, kept):
        clips.assessments.save(clip_id, Assessment(label="urine"))
    clips.dismissals.dismiss([flagged])

    ids = {row["id"] for row, _ in cleanup_candidates(clips, 7)}

    assert flagged in ids
    assert kept not in ids


def test_scene_with_only_dismissed_clips_shows_their_period(tmp_path):
    clips = ClipStore(tmp_path)
    older = ready(clips, days_ago=12)
    newer = ready(clips, days_ago=11)
    ready(clips, days_ago=1, scene="new")
    clips.dismissals.dismiss([older, newer])
    rows = clips.list(include_assessments=True)

    (_, old) = scene_groups(pending_items([], rows), rows)

    assert (old.waiting, old.dismissed, old.thumbnail) == (0, 2, newer)
    assert old.first < old.last
