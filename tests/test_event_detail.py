"""検知の一覧を動画の一覧へまとめ、/records/events を1件の記録の詳細にしたことを検証する。"""

import re
from urllib.parse import urlencode

from support import csrf_token, exchange, request

from poop_detector.video.clips import ClipStore


def text(server, path):
    status, body = request(server, "GET", path)
    assert status == 200, path
    return body.decode()


def test_event_list_moves_to_the_detected_clips(gui):
    """一覧の URL（古いリンクとブックマーク）は、動画の一覧の差分検知へ移す。"""
    server, _, _ = gui
    for path in ("/records/events", "/records/events?filter=unlabeled", "/?filter=all"):
        status, headers, _ = exchange(server, "GET", path)
        assert status in (301, 302), path
        location = headers["Location"]
        if location.startswith("/records/events"):
            status, headers, _ = exchange(server, "GET", location)
            location = headers["Location"]
        assert location == "/clips?show=detected", path


def test_event_detail_shows_one_record_without_a_list(gui):
    server, _, event_id = gui
    html = text(server, f"/records/events?id={event_id}")
    assert "<h2>記録の詳細</h2>" in html
    assert 'aria-label="検知の一覧"' not in html
    assert "実際にはどうでしたか？" in html
    assert 'href="/records"' in html


def test_event_detail_links_to_its_clip(gui):
    server, _, event_id = gui
    clips = ClipStore(server.app.root)
    clip_id = clips.request(event_id=event_id, source="detected")
    html = text(server, f"/records/events?id={event_id}")
    assert f'href="/clips?id={clip_id}"' in html


def test_unknown_event_detail_is_not_found(gui):
    server, _, _ = gui
    status, body = request(server, "GET", "/records/events?id=missing")
    assert status == 404
    assert "記録が見つかりません" in body.decode()


def test_saving_a_label_returns_to_the_same_record(gui):
    server, _, event_id = gui
    fields = {"id": event_id, "label": "false", "csrf": csrf_token(server)}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    status, response, _ = exchange(server, "POST", "/save", urlencode(fields), headers)
    location = response["Location"]
    assert status == 303
    assert location == "/records/events?" + urlencode({"id": event_id, "saved": "1"})
    assert "保存しました" in text(server, location)


def test_clips_can_be_narrowed_to_detections(gui):
    server, _, event_id = gui
    clips = ClipStore(server.app.root)
    detected = clips.request(event_id=event_id, source="detected")
    visit = clips.request()
    clips.update(visit, source="dog_visit")
    html = text(server, "/clips?show=detected")
    assert re.search(r'<a [^>]*href="/clips\?show=detected"[^>]*>差分検知 1</a>', html)
    assert f"id={detected}" in html
    assert f"id={visit}" not in html


def test_records_pages_link_to_detected_clips_instead_of_the_event_list(gui):
    server, _, _ = gui
    for path in ("/", "/records"):
        html = text(server, path)
        assert 'href="/records/events"' not in html, path
        assert 'href="/clips?show=detected"' in html, path


def test_clip_filters_wrap_instead_of_hiding_the_current_one():
    """スマートフォンの幅でも、選んだ絞り込み（4つ目の差分検知）が右に隠れないよう折り返す。"""
    from pathlib import Path

    style = (
        Path(__file__).resolve().parents[1] / "src/poop_detector/gui/templates/style.css"
    ).read_text()
    rule = re.search(r"\.filter-menu ul\{([^}]*)\}", style)
    assert rule
    assert "flex-wrap:wrap" in rule[1]
