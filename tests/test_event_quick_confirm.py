"""検知の画像の確認を、動画と同じく1回押して保存できることを確かめる（#186）。"""

import re
from pathlib import Path
from urllib.parse import urlencode

from support import csrf_token, exchange, request

TEMPLATES = Path(__file__).resolve().parents[1] / "src/poop_detector/gui/templates"


def quick_form(html: str) -> str | None:
    match = re.search(
        r'<form method="post" action="/save" class="quick-confirm">.*?</form>', html, re.S
    )
    return match[0] if match else None


def event_list(html: str) -> str:
    return html.split('aria-label="イベント一覧">', 1)[1].split("</nav>", 1)[0]


def post_save(server, fields: dict[str, str], page: str = "/records/events"):
    body = urlencode({**fields, "csrf": csrf_token(server, page)})
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    return exchange(server, "POST", "/save", body, form)


def test_unlabeled_event_offers_one_tap_buttons(gui):
    server, _, event = gui
    html = request(server, "GET", f"/records/events?id={event}")[1].decode()
    form = quick_form(html)
    assert form
    assert f'<input type="hidden" name="id" value="{event}">' in form
    for value, text in [
        ("feces", "排便"),
        ("urine", "排尿"),
        ("both", "両方"),
        ("false", "誤検知"),
    ]:
        assert f'name="label" value="{value}"' in form
        assert text in form
    # 候補がないので、4つとも枠線のボタンにする（塗りのボタンは画面に1つまで）。
    assert form.count('class="secondary"') == 4
    assert '<svg class="icon"' in form
    # 日時・メモ・残りの選択は「くわしく入力」にたたむ。
    assert '<details class="clip-more">' in html
    details = html.split('<details class="clip-more">', 1)[1].split("</details>", 1)[0]
    assert 'name="start"' in details
    assert 'name="note"' in details
    assert 'value="unknown"' in details
    assert "保留" in details


def test_labeled_event_opens_the_detailed_form_instead(gui):
    server, store, event = gui
    store.classify(event, "urine")
    html = request(server, "GET", f"/records/events?filter=all&id={event}")[1].decode()
    assert quick_form(html) is None
    assert '<details class="clip-more" open>' in html


def test_one_tap_keeps_times_and_note_and_returns_to_the_list(gui):
    server, store, event = gui
    store.classify(event, "unknown", "2026-11-08T19:00:00+09:00", None, "メモ")
    status, headers, _ = post_save(server, {"id": event, "label": "feces", "filter": "unlabeled"})
    assert status == 303
    assert headers["Location"].startswith("/records/events?")
    row = store.list()[0]
    assert (row["feedback"], row["excretion_type"]) == (1, "feces")
    assert row["actual_start"] == "2026-11-08T19:00:00+09:00"
    assert row["note"] == "メモ"


def test_one_tap_from_the_review_queue_moves_to_the_next_item(gui):
    server, store, event = gui
    page = f"/records/events?filter=unlabeled&id={event}&from=review"
    html = request(server, "GET", page)[1].decode()
    assert '<input type="hidden" name="from" value="review">' in quick_form(html)
    fields = {"id": event, "label": "false", "filter": "unlabeled", "from": "review"}
    status, headers, _ = post_save(server, fields, page)
    assert (status, headers["Location"]) == (303, f"/review/next?after=event:{event}")
    assert store.list()[0]["feedback"] == 0


def test_event_list_rows_have_thumbnails_and_state_chips(gui):
    server, store, event = gui
    store.add_excretion("urine", "2026-11-08T19:00:00+09:00")
    listing = event_list(request(server, "GET", "/records/events?filter=all")[1].decode())
    assert (
        f'<span class="clip-thumb"><img src="/image?id={event}" alt="" loading="lazy"'
        ' decoding="async"></span>'
    ) in listing
    assert '<span class="clip-text">' in listing
    assert '<span class="chip chip-warn">未ラベル</span>' in listing
    assert '<span class="chip chip-ok">排尿（手動）</span>' in listing
    # 画像のない記録はアイコンで見せる。
    assert listing.count('<span class="clip-thumb"><svg class="icon"') == 1


def test_event_page_has_no_inline_style():
    assert "style=" not in (TEMPLATES / "events.html").read_text(encoding="utf-8")
