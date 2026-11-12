"""ホーム（ダッシュボード）と、検知の一覧を記録の下へ移したこと（#86）を検証する。"""

import re

from support import exchange, login


def page(server, path, cookie):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def test_home_is_a_dashboard_without_the_label_form(gui):
    server, _, _ = gui
    html = page(server, "/", login(server, role="member"))
    assert 'id="live-frame"' in html
    assert "今日の記録" in html
    assert re.search(r'<a [^>]*href="/review"[^>]*>確認待ち 1件を確認する</a>', html)
    assert 'action="/save"' not in html
    assert 'href="/records/events"' in html


def test_home_counts_todays_records(gui):
    server, store, event_id = gui
    store.classify(event_id, "feces")
    html = page(server, "/", login(server, role="member"))
    today = re.search(r'aria-labelledby="today-title".*?</section>', html, re.S)[0]
    assert "排便 1件" in today
    assert "排尿 0件" in today
    assert "確認待ちはありません" in html


def test_home_lists_recent_records(gui):
    server, store, event_id = gui
    store.classify(event_id, "urine", note="朝の散歩のあと")
    html = page(server, "/", login(server, role="member"))
    recent = re.search(r'aria-labelledby="recent-title".*?</section>', html, re.S)[0]
    assert "排尿" in recent
    assert f'href="/records/events?filter=all&amp;id={event_id}"' in recent


def test_home_without_records_says_so(gui):
    server, _, _ = gui
    html = page(server, "/", login(server, role="member"))
    assert "確定した記録はまだありません" in html


def test_event_list_moved_under_records(gui):
    server, _, event_id = gui
    cookie = login(server, role="member")
    html = page(server, "/records/events", cookie)
    assert 'action="/save"' in html
    assert event_id in html
    nav = re.search(r'aria-label="メインメニュー".*?</nav>', html, re.S)[0]
    assert re.search(r'aria-current="page">記録', nav)
    assert 'href="/records/events"' in page(server, "/records", cookie)


def test_old_home_links_move_to_the_event_list(gui):
    server, _, event_id = gui
    cookie = login(server, role="member")
    path = f"/?filter=all&id={event_id}"
    status, headers, _ = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 301
    assert headers["Location"] == f"/records/events?filter=all&id={event_id}"


def test_record_edit_links_open_the_event_list(gui):
    server, store, event_id = gui
    store.classify(event_id, "feces")
    html = page(server, "/records", login(server, role="member"))
    assert f'href="/records/events?filter=all&amp;id={event_id}"' in html
