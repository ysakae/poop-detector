"""動画通知から該当位置を開き、スマホで確定できることを確認する。"""

from support import ready_clip, request, submit

from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.clips import ClipStore


def test_notification_view_opens_at_candidate_and_confirms_once(gui):
    server, store, _ = gui
    clips = ClipStore(store.root)
    clip = ready_clip(clips)
    clips.assessments.save(
        clip, Assessment(label="feces", activity="excreting", start_seconds=3, reason="候補")
    )
    status, body = request(server, "GET", f"/clips?id={clip}&notice=1")
    assert status == 200
    assert f"/clip-video?id={clip}#t=3" in body.decode()
    assert "排便・排尿の確認" in body.decode()
    for _ in range(2):
        assert (
            submit(
                server,
                "/clips/quick",
                {"id": clip, "label": "feces", "location": "tray"},
                page=f"/clips?id={clip}&notice=1",
            )[0]
            == 303
        )
    assert len(store.records()) == 1


def test_missing_notification_clip_shows_manual_entry(gui):
    server, _, _ = gui
    status, body = request(server, "GET", "/clips?id=missing&notice=1")
    assert status == 200
    assert "指定された動画を確認できません" in body.decode()
    assert b"/records/events?manual=1#manual-record" in body
