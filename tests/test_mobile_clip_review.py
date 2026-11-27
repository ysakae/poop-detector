"""動画通知から該当位置を開き、スマホで確定できることを確認する。"""

import pytest
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
    assert "うんち・おしっこの確認" in body.decode()
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
    assert b'href="/records/new"' in body


@pytest.mark.parametrize(
    ("label", "start", "end", "actual_start", "actual_end"),
    [
        ("feces", "0", "11.25", "06:10:00", "06:10:11.250000"),
        ("urine", "5.25", "", "06:10:05.250000", None),
        ("both", "", "11", None, "06:10:11"),
    ],
)
def test_quick_confirmation_saves_video_offsets_and_record_times(
    gui, label, start, end, actual_start, actual_end
):
    server, store, _ = gui
    clips = ClipStore(store.root)
    clip = ready_clip(clips)

    fields = {"id": clip, "label": label, "start": start, "end": end}
    assert submit(server, "/clips/quick", fields)[0] == 303
    saved = clips.get(clip)
    assert saved["actual_start_offset"] == (float(start) if start else None)
    assert saved["actual_end_offset"] == (float(end) if end else None)
    (record,) = store.records()
    assert record["excretion_type"] == label
    assert record["actual_start"] == (f"2026-11-09T{actual_start}+00:00" if actual_start else None)
    assert record["actual_end"] == (f"2026-11-09T{actual_end}+00:00" if actual_end else None)


def test_quick_confirmation_rejects_reversed_offsets(gui):
    server, store, _ = gui
    clips = ClipStore(store.root)
    clip = ready_clip(clips)

    fields = {"id": clip, "label": "feces", "start": "12", "end": "11"}
    assert submit(server, "/clips/quick", fields)[0] == 400
    assert clips.get(clip)["label"] is None
    assert store.records() == []


@pytest.mark.parametrize(
    ("candidate_start", "trigger_offset", "show_trigger"),
    [(None, 10, True), (0, 10, False), (None, 0, False)],
)
def test_trigger_seek_is_shown_only_as_a_fallback(
    gui, candidate_start, trigger_offset, show_trigger
):
    server, store, _ = gui
    clips = ClipStore(store.root)
    clip = ready_clip(clips)
    clips.update(clip, trigger_offset=trigger_offset)
    clips.assessments.save(
        clip, Assessment(label="feces", activity="excreting", start_seconds=candidate_start)
    )

    page = request(server, "GET", f"/clips?id={clip}")[1].decode()
    assert ("開始候補へ移動" in page) == (candidate_start is not None)
    assert ("操作・検知の位置へ" in page) == show_trigger


def test_notification_opens_the_requested_clip_while_it_is_recording(gui):
    server, store, _ = gui
    clips = ClipStore(store.root)
    clip = clips.request(source="early_alert")
    clips.update(clip, status="recording")
    ready_clip(clips)
    status, body = request(server, "GET", f"/clips?id={clip}&notice=1")
    assert status == 200
    html = body.decode()
    assert f'<article id="clip-detail" data-pending-clip="{clip}"' in html
    assert "録画・変換中の動画があります" in html
    assert "動画ファイルを確認できません" not in html


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
        ("feces", "うんち"),
        ("urine", "おしっこ"),
        ("both", "両方"),
        ("none", "うんち・おしっこなし"),
    ]:
        assert f'name="label" value="{value}"' in form
        assert text in form
    # 自動判定の候補がなければ、どのボタンも塗りにしない。
    assert form.count('class="secondary"') == 4
    # 細かい入力は、たたんでおく。
    assert '<details class="clip-more">' in html
    assert "ラベルを保存" in html


def test_quick_button_label_can_wrap_separately_from_its_icon(gui):
    server, store, _ = gui
    clip = ready_clip(ClipStore(store.root))
    form = quick_form(request(server, "GET", f"/clips?id={clip}")[1].decode())
    assert form
    assert '<span class="quick-label">うんち・おしっこなし</span></button>' in form


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


def test_one_tap_from_the_video_list_still_moves_to_the_next_item(gui):
    server, store, _ = gui
    clip = ready_clip(ClipStore(store.root))
    html = request(server, "GET", f"/clips?id={clip}")[1].decode()
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
