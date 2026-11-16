"""排泄を記録（/records/new）を、種類のボタンを1回押すだけで保存できることを検証する。"""

import re
from datetime import datetime, timedelta
from urllib.parse import urlencode

from support import csrf_token, exchange, request

from poop_detector.timezones import local_now


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
    for value, text in (("feces", "排便"), ("urine", "排尿"), ("both", "両方")):
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
