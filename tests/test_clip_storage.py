"""録画の集約と、容量表示・保護付き整理の回帰テスト。"""

from datetime import UTC, datetime, timedelta
from threading import Thread

import numpy as np
from support import request, submit

from poop_detector.gui.server import make_server
from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.clip_storage import MIB, cleanup_selected, free_space
from poop_detector.video.clips import ClipStore
from poop_detector.video.recording import MAX_EXTENSION_SECONDS, ClipCollector

FRAME = np.zeros((40, 40, 3), np.uint8)


def test_short_visit_keeps_prebuffer_and_records_after_exit(tmp_path):
    sample_times = []

    def encode(samples, path, fps):
        sample_times.extend(s[0] for s in samples)
        path.write_bytes(b"mp4")
        return len(samples) / fps

    frame = np.zeros((40, 40, 3), np.uint8)
    with ClipCollector(tmp_path, before=2, after=4, fps=1, encoder=encode) as collector:
        for sec in (0, 1, 2):
            collector.feed(frame, now=sec)
        entry = collector.trigger(source="dog_enter", now=2)
        for sec in (3, 4, 5):
            collector.feed(frame, now=sec)
        assert collector.trigger(source="dog_exit", now=5, tray_stay_seconds=2) == entry
        for sec in (6, 7, 8, 9):
            collector.feed(frame, now=sec)
            collector.poll(now=sec)
        collector.drain()
        (visit,) = collector.store.list()
        assert visit["source"] == "dog_visit"
        assert visit["status"] == "ready"
        assert visit["tray_stay_seconds"] == 2
        assert not visit["interrupted"]
        assert sample_times == list(range(10))


def stub_encode(samples, path, fps):
    path.write_bytes(b"mp4")
    return len(samples) / fps


def test_dog_exit_clip_keeps_longest_measured_stay(tmp_path):
    with ClipCollector(tmp_path, after=4, fps=1, encoder=stub_encode) as collector:
        collector.feed(FRAME, now=0)
        clip_id = collector.trigger(source="dog_exit", now=0, tray_stay_seconds=15)
        collector.feed(FRAME, now=1)
        assert collector.trigger(source="dog_exit", now=1, tray_stay_seconds=0) == clip_id
        for sec in range(2, 7):
            collector.feed(FRAME, now=sec)
        collector.poll(now=6)
        collector.drain()
        assert collector.store.get(clip_id)["tray_stay_seconds"] == 15


def test_long_visit_merges_entry_and_exit_while_recording(tmp_path):
    with ClipCollector(tmp_path, after=30, encoder=stub_encode) as collector:
        collector.feed(FRAME, now=2)
        entry = collector.trigger(source="dog_enter", now=2)
        collector.feed(FRAME, now=18)
        assert collector.trigger(source="dog_exit", now=18) == entry
        assert collector.jobs[entry]["source"] == "dog_visit"
        assert collector.jobs[entry]["end"] == 48


def test_overlapping_requests_share_one_clip_with_the_most_urgent_source(tmp_path):
    with ClipCollector(tmp_path, after=30, encoder=stub_encode) as collector:
        collector.feed(FRAME, now=0)
        outside = collector.trigger(source="outside_stay", now=0)
        collector.feed(FRAME, now=2)
        assert collector.trigger(source="dog_enter", now=2) == outside
        assert collector.jobs[outside]["source"] == "dog_enter"
        collector.feed(FRAME, now=4)
        assert collector.trigger(source="early_alert", now=4) == outside
        assert collector.jobs[outside]["source"] == "early_alert"
        # 優先度の低い要求は、きっかけを上書きしない。
        collector.feed(FRAME, now=6)
        assert collector.trigger(source="outside_stay", now=6) == outside
        assert collector.jobs[outside]["source"] == "early_alert"
        assert collector.jobs[outside]["end"] == 36
        for sec in range(7, 40):
            collector.feed(FRAME, now=sec)
            collector.poll(now=sec)
        collector.drain()
        (clip,) = collector.store.list()
        assert clip["source"] == "early_alert"
        assert clip["status"] == "ready"


def test_detection_with_event_keeps_its_own_clip(tmp_path):
    with ClipCollector(tmp_path, after=30, encoder=stub_encode) as collector:
        collector.feed(FRAME, now=0)
        visit = collector.trigger(source="dog_enter", now=0)
        collector.feed(FRAME, now=5)
        detected = collector.trigger("event-1", now=5)
        assert detected
        assert detected != visit


def test_merging_stops_after_the_extension_limit(tmp_path):
    with ClipCollector(tmp_path, after=30, encoder=stub_encode) as collector:
        collector.feed(FRAME, now=0)
        first = collector.trigger(source="outside_stay", now=0)
        for sec in range(20, MAX_EXTENSION_SECONDS + 1, 20):
            collector.feed(FRAME, now=sec)
            assert collector.trigger(source="outside_stay", now=sec) == first
        later = MAX_EXTENSION_SECONDS + 10
        collector.feed(FRAME, now=later)
        assert collector.trigger(source="outside_stay", now=later) not in (None, first)


def test_interrupted_recording_is_not_extended(tmp_path):
    with ClipCollector(tmp_path, after=30, encoder=stub_encode) as collector:
        collector.feed(FRAME, now=0)
        first = collector.trigger(source="dog_enter", now=0)
        collector.feed(FRAME, now=20)
        # カメラの映像が5秒以上届かない間は、録画を延ばさない。
        assert collector.trigger(source="early_alert", now=26) != first


def saved_clip(clips, *, days_ago=10, event_id=None):
    clip_id = clips.request(event_id=event_id)
    relative = f"clips/{clip_id}.mp4"
    path = clips.root / relative
    path.write_bytes(b"video")
    clips.update(clip_id, status="ready", path=relative, duration=1)
    requested = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with clips.store.connect() as db:
        db.execute("UPDATE clips SET requested_at=? WHERE id=?", (requested, clip_id))
    return clip_id, path


def test_storage_cleanup_preserves_labels_links_and_newly_pinned_videos(gui):
    server, store, event_id = gui
    clips = ClipStore(store.root)
    target, target_path = saved_clip(clips)
    labeled, labeled_path = saved_clip(clips)
    linked, linked_path = saved_clip(clips, event_id=event_id)
    pinned, pinned_path = saved_clip(clips)
    positive, positive_path = saved_clip(clips)
    recent, recent_path = saved_clip(clips, days_ago=1)
    clips.labels.save(labeled, ClipLabel("none"))
    clips.assessments.save(positive, Assessment(label="urine"))
    clips.assessments.save(target, Assessment(label="none", reason="映像を確認しました"))

    status, page = request(server, "GET", "/manage/storage")
    assert status == 200
    assert "空く容量".encode() in page
    for clip_id in (target, pinned):
        assert f'value="{clip_id}"'.encode() in page
    for clip_id in (labeled, linked, positive, recent):
        assert f'value="{clip_id}"'.encode() not in page
    assert submit(server, "/clips/protect", {"id": pinned, "protected": "1"})[0] == 303
    assert submit(server, "/manage/storage/cleanup", {"id": target, "days": "7"})[0] == 400
    assert target_path.exists()

    assert (
        submit(
            server,
            "/manage/storage/cleanup",
            {
                "id": [target, pinned, linked, labeled, positive, recent],
                "days": "7",
                "confirm": "1",
            },
        )[0]
        == 303
    )
    assert not target_path.exists()
    assert clips.get(target)["status"] == "removed"
    assert clips.assessments.get(target)["reason"] == "映像を確認しました"
    assert all(
        path.exists()
        for path in (pinned_path, linked_path, labeled_path, positive_path, recent_path)
    )


def test_capacity_warnings_follow_configured_limit(tmp_path):
    clips = ClipStore(tmp_path)
    _, path = saved_clip(clips)
    with make_server(tmp_path, 0, disk_mb=8) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for percentage in (75, 90):
                with path.open("r+b") as video:
                    video.truncate((8 * MIB * percentage + 99) // 100)
                status, body = request(server, "GET", "/clips/capacity")
                assert status == 200
                assert f"録画容量が{percentage}％に達しました".encode() in body
        finally:
            server.shutdown()
            thread.join()


def test_cleanup_refuses_a_symlinked_clip_directory(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "clips").symlink_to(outside, target_is_directory=True)
    clips = ClipStore(root)
    clip_id, path = saved_clip(clips)
    assert cleanup_selected(clips, [clip_id], 7) == (0, 1)
    assert path.is_file()


def test_cleanup_can_resume_after_unlink_before_metadata_update(tmp_path):
    clips = ClipStore(tmp_path)
    clip_id, path = saved_clip(clips)
    clips.update(clip_id, status="removing")
    path.unlink()
    assert cleanup_selected(clips, [clip_id], 7) == (1, 0)
    assert clips.get(clip_id)["status"] == "removed"
    assert clips.get(clip_id)["path"] is None


def sized_clip(clips, size, *, source="outside_stay", days_ago=3, event_id=None):
    clip_id, path = saved_clip(clips, days_ago=days_ago, event_id=event_id)
    path.write_bytes(b"v" * size)
    with clips.store.connect() as db:
        db.execute("UPDATE clips SET source=? WHERE id=?", (source, clip_id))
    return clip_id, path


def test_free_space_removes_low_priority_old_videos_until_below_target(tmp_path):
    clips = ClipStore(tmp_path)
    old_outside, old_outside_path = sized_clip(clips, 100, days_ago=5)
    new_outside, new_outside_path = sized_clip(clips, 100, days_ago=2)
    visit, visit_path = sized_clip(clips, 700, source="dog_visit", days_ago=6)
    alert, alert_path = sized_clip(clips, 50, source="early_alert", days_ago=7)
    # 使用量 950 / 上限 1000。目安（80％）を下回るまで、トレー外の古い順に消す。
    assert free_space(clips, 1000) == 2
    assert not old_outside_path.exists()
    assert not new_outside_path.exists()
    assert visit_path.exists()
    assert alert_path.exists()
    statuses = {row["id"]: row["status"] for row in clips.list()}
    assert statuses[old_outside] == statuses[new_outside] == "removed"
    assert statuses[visit] == statuses[alert] == "ready"


def test_free_space_keeps_protected_recent_and_linked_videos(gui):
    _, store, event_id = gui
    clips = ClipStore(store.root)
    _, recent_path = sized_clip(clips, 400, days_ago=0)
    _, linked_path = sized_clip(clips, 400, event_id=event_id)
    labeled, labeled_path = sized_clip(clips, 150)
    clips.labels.save(labeled, ClipLabel("none"))
    assert free_space(clips, 1000) == 0
    assert recent_path.exists()
    assert linked_path.exists()
    assert labeled_path.exists()


def test_free_space_waits_until_usage_reaches_the_start_ratio(tmp_path):
    clips = ClipStore(tmp_path)
    _, path = sized_clip(clips, 800)
    assert free_space(clips, 1000) == 0
    assert path.exists()


def encode_bytes(size):
    def encode(samples, path, fps):
        path.write_bytes(b"m" * size)
        return len(samples) / fps

    return encode


def record_one(collector, source="outside_stay", event_id=None):
    collector.feed(FRAME, now=0)
    clip_id = collector.trigger(event_id, source=source, now=0)
    for sec in (1, 2):
        collector.feed(FRAME, now=sec)
        collector.poll(now=sec)
    collector.drain()
    return next(row for row in collector.store.list() if row["id"] == clip_id)


def test_collector_frees_space_before_saving(tmp_path):
    clips = ClipStore(tmp_path)
    _, old_path = sized_clip(clips, 900)
    disk_mb = 1000 / MIB
    with ClipCollector(
        tmp_path, before=1, after=1, fps=1, disk_mb=disk_mb, encoder=encode_bytes(50)
    ) as collector:
        clip = record_one(collector)
    assert clip["status"] == "ready"
    assert not old_path.exists()


def test_collector_without_auto_cleanup_keeps_old_videos(tmp_path):
    clips = ClipStore(tmp_path)
    _, old_path = sized_clip(clips, 900)
    with ClipCollector(
        tmp_path,
        before=1,
        after=1,
        fps=1,
        disk_mb=1000 / MIB,
        auto_cleanup=False,
        encoder=encode_bytes(120),
    ) as collector:
        clip = record_one(collector)
    assert clip["status"] == "failed"
    assert old_path.exists()


def test_reserve_near_the_limit_is_kept_for_detections(tmp_path):
    clips = ClipStore(tmp_path)
    # 消してよい動画がない（直近の動画だけ）まま、上限の95％を超える。
    sized_clip(clips, 930, days_ago=0)
    options = {"before": 1, "after": 1, "fps": 1, "disk_mb": 1000 / MIB}
    with ClipCollector(tmp_path, encoder=encode_bytes(30), **options) as collector:
        assert record_one(collector)["status"] == "failed"
    with ClipCollector(tmp_path, encoder=encode_bytes(30), **options) as collector:
        assert record_one(collector, source="detected", event_id="event-1")["status"] == "ready"
