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


def quick_form(html):
    import re

    match = re.search(r'<form method="post" action="/clips/quick".*?</form>', html, re.S)
    return match[0] if match else None


def test_unlabeled_clip_offers_one_tap_buttons_without_a_notification(gui):
    """通知から開かなくても、まだ確かめていない動画には大きなボタンを出す（#141）。"""
    server, store, _ = gui
    clip = ready_clip(ClipStore(store.root))
    html = request(server, "GET", f"/clips?id={clip}")[1].decode()
    form = quick_form(html)
    assert form
    for value, text in [
        ("feces", "排便"),
        ("urine", "排尿"),
        ("both", "両方"),
        ("none", "排泄なし"),
    ]:
        assert f'name="label" value="{value}"' in form
        assert text in form
    # 自動判定の候補がなければ、どのボタンも塗りにしない。
    assert form.count('class="secondary"') == 4
    # 細かい入力は、たたんでおく。
    assert '<details class="clip-more">' in html
    assert "動画ラベルのみ保存" in html


def test_suggested_label_is_the_only_filled_button(gui):
    server, store, _ = gui
    clips = ClipStore(store.root)
    clip = ready_clip(clips)
    clips.assessments.save(clip, Assessment(label="urine", activity="excreting", reason="候補"))
    form = quick_form(request(server, "GET", f"/clips?id={clip}")[1].decode())
    assert '<button name="label" value="urine">' in form
    assert form.count('class="secondary"') == 3


def test_labeled_clip_opens_the_detailed_form_instead(gui):
    server, store, _ = gui
    clip = ready_clip(ClipStore(store.root))
    assert submit(server, "/clips/quick", {"id": clip, "label": "none"})[0] == 303
    html = request(server, "GET", f"/clips?id={clip}")[1].decode()
    assert quick_form(html) is None
    assert '<details class="clip-more" open>' in html


def test_one_tap_from_the_review_queue_moves_to_the_next_item(gui):
    server, store, _ = gui
    clip = ready_clip(ClipStore(store.root))
    html = request(server, "GET", f"/clips?id={clip}&from=review")[1].decode()
    assert '<input type="hidden" name="from" value="review">' in quick_form(html)
    from urllib.parse import urlencode

    from support import csrf_token, exchange

    body = urlencode(
        {"id": clip, "label": "none", "from": "review", "csrf": csrf_token(server, "/clips")}
    )
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    status, headers, _ = exchange(server, "POST", "/clips/quick", body, form)
    assert status == 303
    assert headers["Location"] == f"/review/next?after=clip:{clip}"
