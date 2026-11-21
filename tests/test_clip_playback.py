"""動画の画面の再生の操作（最初の1コマ、10秒の前後、操作・検知の位置、2倍速）を検証する。"""

import re
from pathlib import Path

from support import exchange, login

STATIC = Path(__file__).resolve().parents[1] / "src" / "poop_detector" / "gui" / "static"


def ready_clip(server, **values):
    clips = server.app.clips
    clip_id = clips.request(**values)
    clips.update(
        clip_id, status="ready", path=f"clips/{clip_id}.mp4", duration=60, trigger_offset=30.5
    )
    return clip_id


def page(server, path):
    status, _, body = exchange(server, "GET", path, headers=login(server), auth=False)
    assert status == 200, path
    return body.decode()


def test_video_shows_the_thumbnail_before_it_loads(gui):
    server, _, _ = gui
    clip_id = ready_clip(server, source="dog_enter")
    video = re.search(r"<video[^>]*>", page(server, f"/clips?id={clip_id}"))[0]
    assert f'poster="/clip-thumbnail?id={clip_id}"' in video
    assert 'preload="metadata"' in video


def test_playback_controls_sit_under_the_video(gui):
    server, _, _ = gui
    clip_id = ready_clip(server, source="dog_enter")
    html = page(server, f"/clips?id={clip_id}")
    controls = re.search(r'<div class="video-controls"[^>]*>.*?</div>', html, re.S)[0]
    assert 'data-video-jump="-10"' in controls
    assert 'data-video-jump="10"' in controls
    # 操作・検知の位置へは、動画内の位置で移る。
    assert re.search(r'data-video-seek="30\.5"[^>]*>操作・検知の位置へ</button>', controls)
    assert re.search(r'data-video-rate="2" aria-pressed="false"[^>]*>2倍速</button>', controls)
    # ボタンはフォームを送らない。
    assert controls.count('type="button"') == controls.count("<button")


def test_script_handles_the_controls_and_keeps_the_speed_between_clips():
    script = (STATIC / "clips.js").read_text("utf-8")
    for needle in ("data-video-jump", "data-video-rate", "playbackRate", "aria-pressed"):
        assert needle in script
    # 一覧から別の動画を開いたときも、選んだ速さを引き継ぐ。
    assert "applyRate(" in script
