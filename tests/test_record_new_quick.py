"""排泄を記録（/records/new）を、種類のボタンを1回押すだけで保存できることを検証する。"""

import re
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pytest
from support import csrf_token, exchange, ready_clip, request, submit

from poop_detector.records.export import records_csv, summarize_period
from poop_detector.timezones import local_now
from poop_detector.video.clips import ClipStore


def new_record_page(server):
    status, body = request(server, "GET", "/records/new")
    assert status == 200
    return body.decode()


def test_start_is_filled_with_the_current_time(gui):
    server, _, _ = gui
    page = new_record_page(server)
    value = re.search(r'name="start" value="([^"]+)"', page)[1]
    shown = datetime.fromisoformat(value).astimezone()
    assert abs(shown - local_now()) < timedelta(minutes=2)


def test_excretion_type_is_chosen_with_buttons(gui):
    server, _, _ = gui
    page = new_record_page(server)
    assert '<select name="type"' not in page
    for value, text in (("feces", "うんち"), ("urine", "おしっこ"), ("both", "両方")):
        assert re.search(rf'<button[^>]* name="type" value="{value}">.*?{text}</button>', page)


def test_record_kind_is_asked_in_plain_words(gui):
    server, _, _ = gui
    page = new_record_page(server)
    assert "通知は来ましたか？" in page
    assert re.search(r'<input type="radio" name="action" value="manual" checked>', page)
    assert re.search(r'<input type="radio" name="action" value="missed">', page)
    assert "通常の手動記録" not in page
    assert '<select name="action"' not in page


def test_pressing_a_type_saves_with_the_prefilled_time(gui):
    server, store, _ = gui
    page = new_record_page(server)
    start = re.search(r'name="start" value="([^"]+)"', page)[1]
    fields = {"action": "manual", "type": "urine", "start": start, "csrf": csrf_token(server)}
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    status, headers, _ = exchange(server, "POST", "/save", urlencode(fields), form)
    assert (status, headers["Location"]) == (303, "/records?saved=1")
    saved = store.records()[-1]
    assert saved["excretion_type"] == "urine"
    assert saved["kind"] == "MANUAL"


@pytest.mark.parametrize(("kind", "action"), [("feces", "manual"), ("urine", "missed")])
def test_outside_pen_is_recorded_and_listed_as_manual(gui, kind, action):
    server, store, _ = gui
    page = new_record_page(server)
    assert '<option value="outside_pen">サークル外</option>' in page
    start = re.search(r'name="start" value="([^"]+)"', page)[1]
    fields = {"action": action, "type": kind, "start": start, "location": "outside_pen"}
    assert submit(server, "/save", fields)[0] == 303

    (saved,) = store.records()
    assert (saved["excretion_type"], saved["location"], saved["kind"]) == (
        kind,
        "outside_pen",
        "MANUAL",
    )
    assert store.metrics()["missed"] == 0
    assert store.metrics()["excretion_counts"][kind] == 1
    assert "サークル外".encode() in request(server, "GET", "/records")[1]

    day = datetime.fromisoformat(start).date()
    assert ",サークル外," in records_csv([saved], day, day).decode("utf-8-sig")
    assert summarize_period([saved], day, day).locations == {"サークル外": 1}

    _, details = request(server, "GET", f"/records/events?filter=manual&id={saved['id']}")
    assert '<option value="outside_pen" selected>サークル外</option>' in details.decode()
    assert (
        submit(
            server,
            "/save",
            {"id": saved["id"], "label": kind, "location": "outside_pen", "note": "目視"},
        )[0]
        == 303
    )
    assert store.records()[0]["location"] == "outside_pen"


def test_missed_record_can_be_corrected_to_outside_pen(gui):
    server, store, _ = gui
    record = store.add_excretion("urine", local_now().isoformat(), missed=True, location="outside")
    assert store.metrics()["missed"] == 1
    fields = {"id": record, "label": "urine", "location": "outside_pen"}
    assert submit(server, "/save", fields)[0] == 303
    assert store.metrics()["missed"] == 0
    assert store.records()[0]["kind"] == "MANUAL"


def test_outside_pen_record_cannot_be_linked_to_monitoring_video(gui):
    server, store, _ = gui
    stamp = local_now().isoformat()
    record = store.add_excretion("feces", stamp, location="outside_pen")
    inside = store.add_excretion("feces", stamp, location="tray")
    clips = ClipStore(store.root)
    clip_id = ready_clip(clips)
    clips.update(clip_id, start_at=stamp)
    clip = clips.get(clip_id)
    candidates = {candidate["id"] for candidate in clips.labels.record_candidates(clip)}
    assert record not in candidates
    assert inside in candidates
    fields = {"id": clip_id, "label": "feces", "record_id": record}
    assert submit(server, "/clips/confirm", fields)[0] == 400
    assert clips.labels.confirmation(clip_id) is None

    assert submit(server, "/clips/confirm", {**fields, "record_id": inside})[0] == 303
    _, details = request(server, "GET", f"/records/events?filter=manual&id={inside}")
    assert b'value="outside_pen"' not in details
    assert (
        submit(server, "/save", {"id": inside, "label": "feces", "location": "outside_pen"})[0]
        == 400
    )
    assert clips.labels.confirmation(clip_id) == inside
