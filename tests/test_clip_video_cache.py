"""録画の動画を、本人の端末にだけ1日預けてもらうこと（見返すたびに取り直さない）を検証する。"""

import os
import re

from support import exchange

from poop_detector.video.clips import ClipStore


def saved_clip(store, data=b"0123456789abcdef"):
    clips = ClipStore(store.root)
    clip = clips.request()
    (store.root / "clips" / f"{clip}.mp4").write_bytes(data)
    clips.update(clip, status="ready", path=f"clips/{clip}.mp4", duration=10)
    return clip


def test_video_is_kept_on_the_device_for_a_day(gui):
    server, store, _ = gui
    clip = saved_clip(store)
    for headers in ({}, {"Range": "bytes=2-5"}):
        status, sent, _ = exchange(server, "GET", f"/clip-video?id={clip}", headers=headers)
        assert status in (200, 206)
        # 共有のキャッシュ（プロキシ）には残さず、消した録画を長く残さないよう1日にとどめる。
        assert sent["Cache-Control"] == "private, max-age=86400"
        assert re.fullmatch(r'"[0-9a-f]+-[0-9a-f]+"', sent["ETag"])


def test_unchanged_video_is_not_sent_again(gui):
    server, store, _ = gui
    clip = saved_clip(store)
    path = f"/clip-video?id={clip}"
    tag = exchange(server, "GET", path)[1]["ETag"]
    for headers in ({}, {"Range": "bytes=2-5"}):
        status, sent, body = exchange(
            server, "GET", path, headers={**headers, "If-None-Match": tag}
        )
        assert status == 304
        assert body == b""
        assert sent["ETag"] == tag


def test_rewritten_video_gets_a_new_tag(gui):
    server, store, _ = gui
    clip = saved_clip(store)
    path = f"/clip-video?id={clip}"
    tag = exchange(server, "GET", path)[1]["ETag"]
    video = store.root / "clips" / f"{clip}.mp4"
    video.write_bytes(b"another video data")
    os.utime(video, ns=(video.stat().st_atime_ns, video.stat().st_mtime_ns + 10**9))
    status, sent, body = exchange(server, "GET", path, headers={"If-None-Match": tag})
    assert status == 200
    assert body == b"another video data"
    assert sent["ETag"] != tag


def test_missing_and_refused_videos_are_not_kept(gui):
    server, _, _ = gui
    status, sent, _ = exchange(server, "GET", "/clip-video?id=nothing")
    assert status == 404
    assert sent["Cache-Control"] == "no-store"
