"""ホーム（ダッシュボード）と、検知の一覧を記録の下へ移したこと（#86）を検証する。"""

import re
from pathlib import Path

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
    assert "確定記録はまだありません" in html


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
        ("/records/new", "排泄"),
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


GUI_DIR = Path(__file__).resolve().parents[1] / "src" / "poop_detector" / "gui"


def test_home_live_view_has_video_like_controls(gui):
    """ライブ映像は、動画の画面と同じく映像の下端の操作バーの右に全画面のボタンを持つ。"""
    server, _, _ = gui
    html = page(server, "/", login(server, role="member"))
    controls = re.search(r'<div class="live-controls"[^>]*>(.*?)</div>', html, re.S)
    assert controls, "映像の操作バーがありません"
    button = re.search(r'<button [^>]*id="live-maximize"[^>]*>', controls[1])
    assert button, "全画面のボタンがありません"
    assert 'type="button"' in button[0]
    assert 'aria-label="全画面表示"' in button[0]
    assert 'aria-pressed="false"' in button[0]
    # 文字のボタンではなく、動画の操作バーと同じくアイコンだけにする。
    assert "映像を最大化" not in html
    assert re.search(r'<div class="live-controls"[^>]*\bhidden\b', html)


def test_live_script_supports_fullscreen_and_fallback():
    """全画面の API がない端末（iPhone の Safari）でも、画面いっぱいに広げられる。"""
    script = (GUI_DIR / "static" / "live.js").read_text()
    assert "requestFullscreen" in script
    assert "fullscreenchange" in script
    assert "live-maximized" in script
    assert '"Escape"' in script
    # 動画と同じく、映像のダブルクリックでも全画面を切り替える。
    assert '"dblclick"' in script
    assert "全画面を終了" in script


def test_live_script_keeps_the_screen_awake_while_maximized():
    """最大化の間は Screen Wake Lock で画面を消さず、タブに戻ったら取り直し、戻したら手放す。"""
    script = (GUI_DIR / "static" / "live.js").read_text()
    assert 'navigator.wakeLock.request("screen")' in script
    # Wake Lock のないブラウザーでも動くよう、使う前に確かめる。
    assert '"wakeLock" in navigator' in script
    assert ".release()" in script
    # 画面が隠れるとブラウザーが自動で手放すので、見える状態に戻ったら取り直す。
    visibility = re.search(r'"visibilitychange", \(\) => \{(.*?)\n\}\);', script, re.S)
    assert visibility, "visibilitychange の処理がありません"
    assert "keepAwake" in visibility[1]
    # 最大化の表示を切り替える1か所で、取得と解放を決める。
    shown = re.search(r"function showMaximized\(maximized\) \{(.*?)\n\}", script, re.S)
    assert shown, "showMaximized がありません"
    assert "keepAwake(maximized)" in shown[1]


def test_maximized_live_image_is_not_capped_by_the_figure_height():
    """最大化の間は、figure の画像の高さの上限（62vh）を外して画面いっぱいに広げる。"""
    style = (GUI_DIR / "templates" / "style.css").read_text()
    rule = re.search(r"\.live-view\.live-maximized img\{([^}]*)\}", style)
    assert rule, "最大化した映像のスタイルがありません"
    assert "max-height:none" in rule[1]


def test_home_shows_latest_care_by_occurrence_time(gui):
    from datetime import timedelta

    from poop_detector.records.care import CareEntry, save_entry
    from poop_detector.timezones import local_now

    server, store, _ = gui
    cookie = login(server, role="member")
    yesterday = (local_now() - timedelta(days=1)).replace(hour=23, minute=20, second=0)
    old = yesterday.replace(hour=19)
    database = store.database
    save_entry(database, CareEntry("", yesterday, "feeding", "家族A", "", "", 250))
    save_entry(database, CareEntry("", old, "feeding", "家族B", "", "", 100))
    save_entry(database, CareEntry("", yesterday, "sheet", "家族B", ""))
    store.add_excretion("both", yesterday.isoformat())
    store.add_excretion("feces", old.isoformat())

    latest = re.search(
        r'aria-labelledby="last-care-title".*?</section>', page(server, "/", cookie), re.S
    )
    assert latest is not None
    html = latest[0]
    assert html.count("きのう 23:20") == 4
    assert "25 g" in html
    assert "10 g" not in html

    newer = yesterday.replace(hour=23, minute=40)
    save_entry(database, CareEntry("", newer, "feeding", "家族B", ""))
    latest = re.search(
        r'aria-labelledby="last-care-title".*?</section>', page(server, "/", cookie), re.S
    )
    assert latest is not None
    assert "きのう 23:40" in latest[0]
    assert "25 g" not in latest[0]


def test_home_shows_care_without_records_as_empty(gui):
    server, _, _ = gui
    html = page(server, "/", login(server, role="member"))
    latest = re.search(r'aria-labelledby="last-care-title".*?</section>', html, re.S)
    assert latest is not None
    assert latest[0].count("まだ記録がありません") == 4
