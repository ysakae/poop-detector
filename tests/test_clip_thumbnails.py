"""動画のサムネイルを作る・返す・一覧に出す・録画と一緒に消すことの回帰テスト。"""

import zipfile
from datetime import UTC, datetime, timedelta

import cv2
import numpy as np
from support import request, write_video

from poop_detector.records.backup import ALLOWED, create_backup
from poop_detector.video.clip_storage import cleanup_selected
from poop_detector.video.clips import ClipStore
from poop_detector.video.recording import ClipCollector
from poop_detector.video.thumbnails import WIDTH, ensure_thumbnail, thumbnail_path


def playable_clip(clips, *, days_ago=0):
    """再生できる人工の動画を1本登録し、(ID, 動画のパス) を返す。"""
    clip_id = clips.request()
    relative = f"clips/{clip_id}.mp4"
    frame = np.full((180, 320, 3), (60, 120, 200), np.uint8)
    write_video(clips.root / relative, frame)
    clips.update(clip_id, status="ready", path=relative, duration=2, trigger_offset=1)
    requested = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with clips.store.connect() as db:
        db.execute("UPDATE clips SET requested_at=? WHERE id=?", (requested, clip_id))
    return clip_id, clips.root / relative


def test_thumbnail_is_a_small_jpeg_next_to_the_video(tmp_path):
    clips = ClipStore(tmp_path)
    clip_id, video = playable_clip(clips)

    path = ensure_thumbnail(clips, clips.get(clip_id))

    assert path == video.with_suffix(".jpg") == thumbnail_path(video)
    image = cv2.imread(str(path))
    assert image.shape[1] == WIDTH
    assert image.shape[0] == WIDTH * 180 // 320
    # 2回目は作り直さず、同じファイルを返す。
    before = path.stat().st_mtime_ns
    assert ensure_thumbnail(clips, clips.get(clip_id)) == path
    assert path.stat().st_mtime_ns == before


def test_unreadable_or_missing_video_has_no_thumbnail(tmp_path):
    clips = ClipStore(tmp_path)
    broken = clips.request()
    (tmp_path / "clips" / f"{broken}.mp4").write_bytes(b"not a video")
    clips.update(broken, status="ready", path=f"clips/{broken}.mp4", duration=1)
    queued = clips.request()

    assert ensure_thumbnail(clips, clips.get(broken)) is None
    assert ensure_thumbnail(clips, clips.get(queued)) is None
    assert ensure_thumbnail(clips, None) is None
    assert list((tmp_path / "clips").glob("*.jpg")) == []


def test_recording_saves_a_thumbnail_with_the_video(tmp_path):
    frame = np.full((90, 160, 3), 128, np.uint8)

    def encode(samples, path, fps):
        write_video(path, frame)
        return len(samples) / fps

    with ClipCollector(tmp_path, before=1, after=1, fps=1, encoder=encode) as collector:
        collector.feed(frame, now=0)
        clip_id = collector.trigger(source="detected", now=0)
        for sec in (1, 2):
            collector.feed(frame, now=sec)
            collector.poll(now=sec)
        collector.drain()
        assert collector.store.get(clip_id)["status"] == "ready"

    assert (tmp_path / "clips" / f"{clip_id}.jpg").is_file()


def test_thumbnail_route_serves_the_image_only_to_signed_in_people(gui):
    server, store, _ = gui
    clips = ClipStore(store.root)
    clip_id, video = playable_clip(clips)

    status, body = request(server, "GET", f"/clip-thumbnail?id={clip_id}")
    assert status == 200
    assert body.startswith(b"\xff\xd8")
    assert video.with_suffix(".jpg").is_file()

    assert request(server, "GET", f"/clip-thumbnail?id={clip_id}", auth=False)[0] == 403
    assert request(server, "GET", "/clip-thumbnail?id=../../events")[0] == 404
    assert request(server, "GET", "/clip-thumbnail?id=unknown")[0] == 404


def test_list_shows_thumbnails_and_a_state_label(gui):
    server, store, _ = gui
    clips = ClipStore(store.root)
    clip_id, _ = playable_clip(clips)
    waiting = clips.request()

    page = request(server, "GET", "/clips")[1].decode()

    assert f'<img src="/clip-thumbnail?id={clip_id}" alt=""' in page
    assert f"/clip-thumbnail?id={waiting}" not in page
    assert "人のラベルなし" not in page
    assert 'title="自動判定の結果なし"' in page
    assert 'aria-hidden="true">—</span>' in page


def test_cleanup_removes_the_thumbnail_with_the_video(tmp_path):
    clips = ClipStore(tmp_path)
    old, old_video = playable_clip(clips, days_ago=10)
    recent, recent_video = playable_clip(clips, days_ago=1)
    for clip_id in (old, recent):
        assert ensure_thumbnail(clips, clips.get(clip_id)) is not None

    assert cleanup_selected(clips, [old, recent], 7) == (1, 1)

    assert not old_video.exists()
    assert not old_video.with_suffix(".jpg").exists()
    assert recent_video.with_suffix(".jpg").exists()
    assert ensure_thumbnail(clips, clips.get(old)) is None


def test_backup_leaves_thumbnails_out(tmp_path):
    """サムネイルは動画から作り直せるので、バックアップには入れない。"""
    data = tmp_path / "data"
    clips = ClipStore(data)
    clip_id, _ = playable_clip(clips)
    ensure_thumbnail(clips, clips.get(clip_id))
    target = tmp_path / "backup.zip"

    summary = create_backup(data, target, include_clips=True)

    assert summary.clips == 1
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    assert f"clips/{clip_id}.mp4" in names
    assert f"clips/{clip_id}.jpg" not in names
    # 復元も、サムネイルを含むバックアップを受け付けない。
    assert ALLOWED.fullmatch(f"clips/{clip_id}.jpg") is None
