"""確認待ちの画面と、保存すると次の項目へ進む流れを検証する。"""

import re
from urllib.parse import urlencode

import numpy as np
from support import exchange, login

from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clip_reviews import Assessment

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def page(server, path, cookie):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def post(server, cookie, path, fields, page_path):
    html = page(server, page_path, cookie)
    token = re.search(r'name="csrf" value="([^"]+)"', html)[1]
    body = urlencode({**fields, "csrf": token})
    return exchange(server, "POST", path, body, {**FORM, **cookie}, auth=False)


def ready_clip(server, **values):
    clips = server.app.clips
    clip_id = clips.request(**values)
    clips.update(clip_id, status="ready", path=f"clips/{clip_id}.mp4", duration=30)
    return clip_id


def test_review_page_lists_what_waits_for_review(gui):
    server, _, event_id = gui
    clip_id = ready_clip(server, source="manual")
    cookie = login(server, role="member")
    html = page(server, "/review", cookie)
    assert "確認待ち 2件" in html
    assert f'href="/clips?id={clip_id}&amp;from=review"' in html
    assert f'href="/records/events?filter=unlabeled&amp;id={event_id}&amp;from=review"' in html
    assert 'href="/review/next"' in html


def test_menu_shows_the_number_waiting(gui):
    server, _, _ = gui
    html = page(server, "/records", login(server, role="member"))
    nav = re.search(r'aria-label="メインメニュー".*?</nav>', html, re.S)[0]
    assert re.search(r'href="/review">確認待ち<span class="badge"[^>]*>1</span>', nav)


def test_empty_review_says_so(gui):
    server, store, event_id = gui
    store.classify(event_id, "false")
    cookie = login(server, role="member")
    html = page(server, "/review", cookie)
    assert "確認待ちはありません" in html
    status, headers, _ = exchange(server, "GET", "/review/next", headers=cookie, auth=False)
    assert (status, headers["Location"]) == (303, "/review?done=1")
    assert "すべて確認しました" in page(server, "/review?done=1", cookie)


def test_next_opens_the_newest_item_then_skips_forward(gui):
    server, _, event_id = gui
    clip_id = ready_clip(server, source="manual")
    cookie = login(server, role="member")
    status, headers, _ = exchange(server, "GET", "/review/next", headers=cookie, auth=False)
    assert (status, headers["Location"]) == (303, f"/clips?id={clip_id}&from=review")
    path = f"/review/next?after=clip:{clip_id}"
    _, headers, _ = exchange(server, "GET", path, headers=cookie, auth=False)
    assert headers["Location"] == f"/records/events?filter=unlabeled&id={event_id}&from=review"


def test_editors_opened_from_review_show_the_review_bar(gui):
    server, _, event_id = gui
    clip_id = ready_clip(server, source="manual")
    cookie = login(server, role="member")
    html = page(server, f"/records/events?filter=unlabeled&id={event_id}&from=review", cookie)
    assert "確認待ち 残り2件" in html
    assert f'href="/review/next?after=event:{event_id}"' in html
    assert 'name="from" value="review"' in html
    html = page(server, f"/clips?id={clip_id}&from=review", cookie)
    assert "確認待ち 残り2件" in html
    assert f'href="/review/next?after=clip:{clip_id}"' in html
    assert 'name="from" value="review"' in html
    # 検知の一覧は、確認待ちから開いていなければ出さない（動画は列にあれば出す）。
    events = page(server, f"/records/events?filter=unlabeled&id={event_id}", cookie)
    assert "確認待ち 残り" not in events


def test_saving_an_event_from_review_moves_to_the_next(gui):
    server, store, event_id = gui
    cookie = login(server, role="member")
    fields = {"id": event_id, "label": "false", "filter": "unlabeled", "from": "review"}
    path = f"/records/events?filter=unlabeled&id={event_id}&from=review"
    status, headers, _ = post(server, cookie, "/save", fields, path)
    assert (status, headers["Location"]) == (303, f"/review/next?after=event:{event_id}")
    assert next(row for row in store.list() if row["id"] == event_id)["feedback"] == 0


def test_saving_a_clip_from_review_moves_to_the_next(gui):
    server, store, event_id = gui
    clip_id = ready_clip(server, event_id=event_id)
    cookie = login(server, role="member")
    fields = {"id": clip_id, "label": "none", "activity": "other", "from": "review"}
    path = f"/clips?id={clip_id}&from=review"
    status, headers, _ = post(server, cookie, "/clips/label", fields, path)
    assert (status, headers["Location"]) == (303, f"/review/next?after=clip:{clip_id}")
    # 動画で排泄なしとしたので、ひも付く検知も誤検知になり、確認待ちから消える。
    assert next(row for row in store.list() if row["id"] == event_id)["feedback"] == 0
    assert "確認待ちはありません" in page(server, "/review", cookie)


def test_saving_outside_review_stays_on_the_editor(gui):
    """確認待ちの列にない動画（ラベル済み）と、検知の一覧からの保存は、同じ画面に残る。"""
    server, _, event_id = gui
    clip_id = ready_clip(server, source="manual")
    cookie = login(server, role="member")
    server.app.clips.labels.save(clip_id, ClipLabel("none", None, None, "other", "", None))
    html = page(server, f"/clips?id={clip_id}", cookie)
    assert 'name="from" value="review"' not in html
    assert "確認待ち 残り" not in html
    fields = {"id": clip_id, "label": "none", "activity": "other"}
    _, headers, _ = post(server, cookie, "/clips/label", fields, f"/clips?id={clip_id}")
    assert headers["Location"] == f"/clips?id={clip_id}&saved=1"
    fields = {"id": event_id, "label": "false", "filter": "unlabeled"}
    _, headers, _ = post(server, cookie, "/save", fields, "/records/events")
    assert headers["Location"].startswith("/records/events?")


def test_waiting_clip_opened_from_the_clip_list_joins_the_review(gui):
    """確認待ちの列にある動画は、動画の一覧から開いても、保存すると次の件へ進む。"""
    server, _, _ = gui
    clip_id = ready_clip(server, source="manual")
    cookie = login(server, role="member")
    for path in (f"/clips?id={clip_id}", "/clips"):
        html = page(server, path, cookie)
        assert "確認待ち 残り2件" in html
        assert f'href="/review/next?after=clip:{clip_id}"' in html
        # ラベルを保存する3つのフォーム（1回押す・くわしく入力・確定）が、次の件へ進む。
        assert html.count('name="from" value="review"') >= 2
        # 動画の一覧から開いたので、一覧と保存のボタンは残す。
        assert 'id="clip-list"' in html
        assert "今の前後の動画を保存" in html
    focused = page(server, f"/clips?id={clip_id}&from=review", cookie)
    assert 'id="clip-list"' not in focused


def test_clip_outside_the_queue_does_not_join_the_review(gui):
    """犬の出入りの動画（排泄の候補でない）は、確認待ちに並ばないので、帯を出さない。"""
    server, _, _ = gui
    clip_id = ready_clip(server, source="dog_enter")
    html = page(server, f"/clips?id={clip_id}", login(server, role="member"))
    assert "確認待ち 残り" not in html
    assert 'name="from" value="review"' not in html


def test_home_links_to_the_review(gui):
    server, _, _ = gui
    html = page(server, "/", login(server, role="member"))
    assert re.search(r'<a [^>]*href="/review"[^>]*>確認待ち 1件を確認する</a>', html)


def test_holding_an_item_moves_past_it_instead_of_reopening_it(gui):
    server, store, event_id = gui
    newer = store.record(np.full((50, 50, 3), 160, np.uint8), 0.1, None, 1)
    cookie = login(server, role="member")
    fields = {"id": newer, "label": "unknown", "filter": "unlabeled", "from": "review"}
    _, headers, _ = post(server, cookie, "/save", fields, f"/records/events?id={newer}&from=review")
    _, headers, _ = exchange(server, "GET", headers["Location"], headers=cookie, auth=False)
    assert headers["Location"] == f"/records/events?filter=unlabeled&id={event_id}&from=review"


def test_after_the_last_item_returns_to_the_list_when_some_were_skipped(gui):
    server, _, event_id = gui
    cookie = login(server, role="member")
    path = f"/review/next?after=event:{event_id}"
    _, headers, _ = exchange(server, "GET", path, headers=cookie, auth=False)
    assert headers["Location"] == "/review?end=1"
    assert "飛ばした件が残っています" in page(server, "/review?end=1", cookie)


def test_saved_item_continues_with_the_older_ones(gui):
    server, store, event_id = gui
    newer = store.record(np.full((50, 50, 3), 160, np.uint8), 0.1, None, 1)
    store.classify(newer, "false")
    cookie = login(server, role="member")
    path = f"/review/next?after=event:{newer}"
    _, headers, _ = exchange(server, "GET", path, headers=cookie, auth=False)
    assert headers["Location"] == f"/records/events?filter=unlabeled&id={event_id}&from=review"


def test_detection_with_a_video_is_listed_as_one_item(gui):
    server, _, event_id = gui
    ready_clip(server, event_id=event_id)
    html = page(server, "/review", login(server, role="member"))
    assert "確認待ち 1件" in html
    assert "差分の検知（動画）" in html


def test_review_shows_only_the_item(gui):
    server, _, event_id = gui
    clip_id = ready_clip(server, source="manual")
    cookie = login(server, role="member")
    html = page(server, f"/clips?id={clip_id}&from=review", cookie)
    assert 'id="clip-list"' not in html
    assert "今の前後の動画を保存" not in html
    html = page(server, f"/records/events?filter=unlabeled&id={event_id}&from=review", cookie)
    assert 'aria-label="イベント一覧"' not in html
    assert "目視で確認した排便・排尿を記録する" not in html
    assert 'aria-label="イベント一覧"' in page(server, "/records/events", cookie)


def test_menu_marks_review_while_reviewing(gui):
    server, _, event_id = gui
    clip_id = ready_clip(server, source="manual")
    cookie = login(server, role="member")
    for path in (f"/clips?id={clip_id}&from=review", f"/records/events?id={event_id}&from=review"):
        html = page(server, path, cookie)
        nav = re.search(r'aria-label="メインメニュー".*?</nav>', html, re.S)[0]
        assert re.search(r'aria-current="page">確認待ち', nav)


def test_review_list_shows_thumbnails_and_what_was_found(gui):
    """動画の一覧と同じ情報（サムネイル・きっかけ・自動判定の候補）で、開く前に見分けられる。"""
    server, _, event_id = gui
    clip_id = ready_clip(server, source="early_alert")
    plain = ready_clip(server, source="manual")
    server.app.clips.assessments.save(
        clip_id,
        Assessment(
            label="feces",
            activity="excreting",
            start_seconds=1,
            end_seconds=2,
            reason="人工データ",
            location="tray",
        ),
    )
    html = page(server, "/review", login(server, role="member"))
    assert f'<img src="/clip-thumbnail?id={clip_id}" alt=""' in html
    assert f'<img src="/image?id={event_id}" alt=""' in html
    assert "トイレ見守りの動画" in html
    assert '<span class="chip chip-warn">自動判定の候補 ／ 排便 ／ トレー内</span>' in html
    row = re.search(rf'<li><a href="/clips\?id={plain}.*?</li>', html, re.S)[0]
    assert '<span class="chip chip-warn">未ラベル</span>' in row


def test_stay_clips_require_an_excretion_candidate(gui):
    server, _, _ = gui
    clips = server.app.clips
    pending = ready_clip(server, source="outside_stay")
    negative = ready_clip(server, source="early_alert")
    clips.assessments.save(negative, Assessment("none", "other", None, None, "人工データ"))
    positive = ready_clip(server, source="outside_stay")
    clips.assessments.save(
        positive, Assessment("urine", "excreting", 1, 2, "人工データ", "outside")
    )
    cookie = login(server, role="member")
    review = page(server, "/review", cookie)
    library = page(server, "/clips?show=unlabeled", cookie)
    assert f"/clips?id={positive}&amp;from=review" in review
    for clip_id in (pending, negative):
        assert f"/clips?id={clip_id}&amp;from=review" not in review
        assert f"/clips?id={clip_id}" in library
