"""動画の画面の上をたたみ、変換待ちの動画を自動で更新する（Issue #187）。"""

import json
import re
from pathlib import Path

from support import exchange, ready_clip, request

from poop_detector.video.clips import ClipStore

STYLE = Path(__file__).parents[1] / "src/poop_detector/gui/templates/style.css"


def clips_html(server, path="/clips"):
    status, body = request(server, "GET", path)
    assert status == 200
    return body.decode()


def tools(html):
    """たたんだ「動画を保存する・録画容量」の中身。"""
    match = re.search(r'<details class="clip-tools"[^>]*>.*?</details>', html, re.S)
    assert match, "保存の操作と録画容量をたたむ details がない"
    return match[0]


def test_save_controls_and_capacity_are_folded_before_the_list(gui):
    server, store, _ = gui
    ready_clip(ClipStore(store.root))
    html = clips_html(server)
    folded = tools(html)
    assert folded.startswith('<details class="clip-tools">')
    assert "<summary>動画を保存する・録画容量" in folded
    assert "今の前後の動画を保存" in folded
    assert 'id="recording-capacity"' in folded
    assert 'href="/clips"' in folded
    # 見出しと案内のあと、たたんだ操作と絞り込みを挟んで、すぐ一覧が来る。
    order = [
        "<h2>動画</h2>",
        '<details class="clip-tools"',
        'class="filter-menu"',
        'id="clip-list"',
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)
    # たたんだ外に、保存の操作と録画容量を重ねて出さない。
    outside = html.replace(folded, "")
    assert "今の前後の動画を保存" not in outside
    assert 'id="recording-capacity"' not in outside


def test_folded_summary_tells_when_saving_is_unavailable(gui):
    server, store, _ = gui
    clips = ClipStore(store.root)
    folded = tools(clips_html(server))
    summary = re.search(r"<summary>.*?</summary>", folded, re.S)[0]
    assert "カメラ接続を確認できません" in summary
    assert "動画収集のカメラ接続を確認できません" in folded

    clips.heartbeat(True)
    folded = tools(clips_html(server))
    assert "カメラ接続を確認できません" not in folded


def test_saved_message_stays_visible_outside_the_fold(gui):
    server, _, _ = gui
    html = clips_html(server, "/clips?saved=1")
    assert "保存しました。" in html
    assert "保存しました。" not in tools(html)


def test_admin_storage_link_is_inside_the_fold(gui):
    server, _, _ = gui
    assert 'href="/manage/storage"' in tools(clips_html(server))


def test_pending_clips_are_marked_for_automatic_refresh(gui):
    server, store, _ = gui
    clips = ClipStore(store.root)
    done = ready_clip(clips)
    waiting = clips.request()
    html = clips_html(server, f"/clips?id={waiting}")
    assert f'data-pending-clip="{waiting}" data-clip-status="queued"' in html
    assert f'data-pending-clip="{done}"' not in html
    detail = re.search(r'<article id="clip-detail"[^>]*>', html)[0]
    assert f'data-pending-clip="{waiting}"' in detail
    # JavaScript が動かないときのために、変換待ちの知らせと「更新」のリンクを出す。
    assert 'id="clip-pending"' in html
    assert '<script src="/assets/clip_status.js" defer></script>' in html

    html = clips_html(server, f"/clips?id={done}")
    detail = re.search(r'<article id="clip-detail"[^>]*>', html)[0]
    assert "data-pending-clip" not in detail


def test_no_pending_notice_when_every_clip_is_settled(gui):
    server, store, _ = gui
    ready_clip(ClipStore(store.root))
    html = clips_html(server)
    assert "data-pending-clip" not in html
    assert 'id="clip-pending"' not in html


def test_focused_review_keeps_the_top_controls_hidden(gui):
    server, store, _ = gui
    clip = ready_clip(ClipStore(store.root))
    html = clips_html(server, f"/clips?id={clip}&from=review")
    assert '<details class="clip-tools"' not in html
    assert "今の前後の動画を保存" not in html


def test_status_route_returns_only_ids_and_states(gui):
    server, store, _ = gui
    clips = ClipStore(store.root)
    waiting = clips.request()
    done = ready_clip(clips)
    path = f"/clips/status?id={waiting}&id={done}&id=missing&id=../../.env"
    status, headers, body = exchange(server, "GET", path)
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    payload = json.loads(body)
    assert sorted(payload["clips"], key=lambda clip: clip["id"]) == sorted(
        [{"id": waiting, "status": "queued"}, {"id": done, "status": "ready"}],
        key=lambda clip: clip["id"],
    )
    assert b"clips/" not in body

    clips.update(waiting, status="encoding")
    payload = json.loads(request(server, "GET", f"/clips/status?id={waiting}")[1])
    assert payload == {"clips": [{"id": waiting, "status": "encoding"}]}


def test_status_route_refuses_without_session_and_from_other_sites(gui):
    server, store, _ = gui
    waiting = ClipStore(store.root).request()
    assert request(server, "GET", f"/clips/status?id={waiting}", auth=False)[0] == 403
    headers = {"Sec-Fetch-Site": "cross-site"}
    assert exchange(server, "GET", f"/clips/status?id={waiting}", headers=headers)[0] == 403


def test_status_route_limits_the_number_of_ids(gui):
    server, store, _ = gui
    clips = ClipStore(store.root)
    ids = [clips.request() for _ in range(3)]
    query = "&".join(f"id={clip}" for clip in ids * 30)
    payload = json.loads(request(server, "GET", "/clips/status?" + query)[1])
    assert len(payload["clips"]) == 3


def test_status_script_polls_only_while_needed(gui):
    server, _, _ = gui
    status, headers, body = exchange(server, "GET", "/assets/clip_status.js")
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")
    script = body.decode()
    assert script.startswith('"use strict";')
    for point in (
        "/clips/status",
        "[data-pending-clip]",
        "visibilitychange",
        "document.hidden",
        "console.warn",
        "location.reload()",
        "paused",
    ):
        assert point in script, point


def test_mobile_list_is_not_squeezed_to_a_few_rows():
    css = STYLE.read_text(encoding="utf-8")
    mobile = css.split("@media(max-width:800px){", 1)[1].split("\n}", 1)[0]
    assert ".workspace #clip-list{max-height:" in mobile
    assert ".clip-tools{" in css


def test_clip_script_works_without_the_list_in_focused_review(gui):
    """確認待ちから開いた画面には一覧がない。一覧がなくても、再生位置のボタンの処理を登録する。"""
    server, _, _ = gui
    script = request(server, "GET", "/assets/clips.js")[1].decode()
    assert "clipList.addEventListener" not in script
    assert "clipList?.addEventListener" in script
