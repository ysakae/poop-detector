"""ホーム（ダッシュボード）と、検知の一覧を記録の下へ移したこと（#86）を検証する。"""

import re

from support import exchange, login


def page(server, path, cookie):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def stat(html, label):
    """大きな数字の部品（_macros.html の stat）から、値と単位を読む。"""
    match = re.search(
        rf'{label}</span><strong class="stat-value">([^<]+)<span class="stat-unit">([^<]+)', html
    )
    assert match, f"{label} の数字がありません"
    return match[1], match[2]


def filled_buttons(html):
    """塗りのボタン（いちばん大事な操作）の数。枠線・文字のボタンは数えない。"""
    return len(re.findall(r'class="button"', html))


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
    assert stat(today, "排便") == ("1", "回")
    assert stat(today, "排尿") == ("0", "回")
    # 種類不明は、あるときだけ出す。
    assert "種類不明" not in today
    assert "確認待ちはありません" in html


def test_home_lists_recent_records(gui):
    server, store, event_id = gui
    store.classify(event_id, "urine", note="朝の散歩のあと")
    html = page(server, "/", login(server, role="member"))
    recent = re.search(r'aria-labelledby="recent-title".*?</section>', html, re.S)[0]
    assert "排尿" in recent
    assert '<svg class="icon"' in recent
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


def test_home_has_one_main_action(gui):
    """1画面に主役は1つ（#141）。確認待ちがあれば、それだけを塗りのボタンにする。"""
    server, store, event_id = gui
    cookie = login(server, role="member")
    html = page(server, "/", cookie)
    assert "確認待ち 1件を確認する" in html
    assert filled_buttons(html) == 1
    store.classify(event_id, "feces")
    assert filled_buttons(page(server, "/", cookie)) == 0


def test_home_records_care_in_one_tap(gui):
    server, _, _ = gui
    html = page(server, "/", login(server, role="member"))
    quick = re.search(r'<nav class="quick" aria-label="記録する">.*?</nav>', html, re.S)[0]
    for href, text in [
        ("/care#feeding", "ごはん"),
        ("/care#sheet", "シート交換"),
        ("/weight", "体重"),
        ("/records/events?manual=1#manual-record", "排泄"),
    ]:
        assert re.search(
            rf'<a href="{re.escape(href)}"><svg class="icon".*?</svg>{text}</a>', quick
        )


def test_home_adds_up_todays_food(gui):
    from poop_detector.records.care import CareEntry, save_entry
    from poop_detector.timezones import local_now

    server, store, _ = gui
    cookie = login(server, role="member")
    today = re.search(
        r'aria-labelledby="today-title".*?</section>', page(server, "/", cookie), re.S
    )
    assert stat(today[0], "ごはん") == ("0", "g")
    for amount in (305, 250):
        save_entry(store.database, CareEntry("", local_now(), "feeding", "家族", "", "", amount))
    save_entry(store.database, CareEntry("", local_now(), "sheet", "家族", ""))
    today = re.search(
        r'aria-labelledby="today-title".*?</section>', page(server, "/", cookie), re.S
    )
    assert stat(today[0], "ごはん") == ("55.5", "g")


def test_home_shows_unspecified_records_only_when_they_exist(gui):
    from poop_detector.timezones import local_now

    server, store, _ = gui
    store.add_excretion("unspecified", local_now().isoformat())
    html = page(server, "/", login(server, role="member"))
    today = re.search(r'aria-labelledby="today-title".*?</section>', html, re.S)[0]
    assert stat(today, "種類不明") == ("1", "回")
