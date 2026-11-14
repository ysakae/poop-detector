"""画面の共通の枠（ナビゲーション、ログイン中の人、監視の状態）と、管理の画面の URL（#86）を検証する。"""

import re

import pytest
from support import exchange, login

from poop_detector.capture.live_preview import LivePreview

MEMBER_SECTIONS = {
    "/": "ホーム",
    "/review": "確認待ち",
    "/records": "記録",
    "/care": "お世話",
    "/weight": "体重",
    "/clips": "動画",
    "/activity": "行動マップ",
}
ADMIN_PAGES = {
    "/manage": "稼働状況とトレー",
    "/manage/setup": "初回セットアップ",
    "/manage/areas": "範囲の登録",
    "/manage/settings": "設定",
    "/manage/checks": "確認と診断",
    "/manage/storage": "動画の保存",
    "/manage/users": "ユーザー",
    "/manage/invite": "招待",
    "/manage/audit": "監査ログ",
}
MOVED = {
    "/clips/storage": "/manage/storage",
    "/account/users": "/manage/users",
    "/account/invite": "/manage/invite",
    "/account/audit": "/manage/audit",
}


def page(server, path, cookie):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200
    return body.decode()


def nav_of(html, label):
    match = re.search(rf'<nav[^>]*aria-label="{label}"[^>]*>(.*?)</nav>', html, re.S)
    assert match, f"{label} がありません"
    return match[1]


@pytest.mark.parametrize("path", sorted(MEMBER_SECTIONS))
def test_every_page_shows_the_main_navigation(gui, path):
    server, _, _ = gui
    html = page(server, path, login(server, "家族B", role="member"))
    nav = nav_of(html, "メインメニュー")
    for href, text in MEMBER_SECTIONS.items():
        assert f'href="{href}"' in nav
        assert text in nav
    assert 'href="/manage"' not in nav
    # いまいる場所だけに印を付ける。
    assert nav.count('aria-current="page"') == 1
    # 確認待ちには件数が付く（<span class="badge">）。
    current = re.search(r'<a [^>]*aria-current="page"[^>]*>([^<]+)', nav)[1]
    assert current == MEMBER_SECTIONS[path]


def test_sub_pages_mark_their_section(gui):
    server, _, _ = gui
    html = page(server, "/clips?id=x", login(server, role="member"))
    current = re.search(r'aria-current="page"[^>]*>([^<]+)</a>', nav_of(html, "メインメニュー"))
    assert current[1] == "動画"


def test_header_shows_who_is_logged_in(gui):
    server, _, _ = gui
    html = page(server, "/records", login(server, "家族B", role="member"))
    assert '<a href="/account">家族Bさん</a>' in html
    assert 'href="/logout"' in html


def test_admins_see_the_management_section(gui):
    server, _, _ = gui
    admin = login(server, "管理者")
    nav = nav_of(page(server, "/", admin), "メインメニュー")
    assert 'href="/manage"' in nav
    html = page(server, "/manage/users", admin)
    current = re.search(r'aria-current="page"[^>]*>([^<]+)</a>', nav_of(html, "メインメニュー"))
    assert current[1] == "管理"


@pytest.mark.parametrize("path", sorted(ADMIN_PAGES))
def test_management_pages_share_a_sub_navigation(gui, path):
    server, _, _ = gui
    sub = nav_of(page(server, path, login(server)), "管理のメニュー")
    for href, text in ADMIN_PAGES.items():
        assert f'href="{href}"' in sub
        assert text in sub
    current = re.search(r'aria-current="page"[^>]*>([^<]+)</a>', sub)
    assert current[1] == ADMIN_PAGES[path]


def test_monitor_status_appears_in_the_header(gui):
    server, _, _ = gui
    cookie = login(server, role="member")
    assert "監視：停止中または未確認" in page(server, "/records", cookie)
    LivePreview(server.app.root).pulse(True)
    assert "監視：稼働中" in page(server, "/records", cookie)
    LivePreview(server.app.root).pulse(False)
    assert "監視：カメラを再接続中" in page(server, "/records", cookie)


def test_pages_without_login_have_no_navigation(gui):
    server, _, _ = gui
    # 127.0.0.1 で開くと localhost へ移すので、localhost で開く。
    host = {"Host": f"localhost:{server.server_address[1]}"}
    status, _, body = exchange(server, "GET", "/login", headers=host, auth=False)
    assert status == 200
    assert "メインメニュー" not in body.decode()


@pytest.mark.parametrize(("old", "new"), sorted(MOVED.items()))
def test_old_bookmarks_move_to_the_management_section(gui, old, new):
    server, _, _ = gui
    status, headers, _ = exchange(
        server, "GET", old + "?days=30", headers=login(server), auth=False
    )
    assert status == 301
    assert headers["Location"] == new + "?days=30"


def test_logout_page_points_to_the_login_screen(gui):
    server, _, _ = gui
    cookie = login(server, role="member")
    html = page(server, "/logout", cookie)
    token = re.search(r'name="csrf" value="([^"]+)"', html)[1]
    form = {"Content-Type": "application/x-www-form-urlencoded", **cookie}
    status, _, body = exchange(server, "POST", "/logout", f"csrf={token}", form, auth=False)
    assert status == 200
    assert "トークン" not in body.decode()
    assert 'href="/login"' in body.decode()


# スマートフォンの幅で画面の下に出すタブ（#141）。主な4つを並べ、ほかは「その他」にまとめる。
TABS = {"/": "ホーム", "/review": "確認待ち", "/records": "記録", "/clips": "動画"}
MORE = {
    "/care": "お世話",
    "/weight": "体重",
    "/activity": "行動マップ",
    "/account": "自分の設定",
    "/logout": "ログアウト",
}


def test_tab_bar_shows_four_places_and_gathers_the_rest(gui):
    server, _, _ = gui
    tabs = nav_of(page(server, "/", login(server, "家族B", role="member")), "タブ")
    bar, more = tabs.split("<details", 1)
    for href, text in TABS.items():
        assert re.search(rf'<a href="{href}"[^>]*><svg class="icon".*?</svg>{text}', bar), text
    assert re.search(r'<a href="/" aria-current="page">', bar)
    assert "その他" in more
    for href, text in MORE.items():
        assert f'href="{href}"' not in bar
        assert re.search(rf'<a href="{href}"[^>]*><svg class="icon".*?</svg>{text}', more), text
    assert 'href="/manage"' not in tabs
    # いまいる場所が「その他」の中でなければ、「その他」には印を付けない。
    assert 'class="tab-more current"' not in tabs


def test_tab_bar_marks_more_when_the_place_is_inside_it(gui):
    server, _, _ = gui
    tabs = nav_of(page(server, "/care", login(server, role="member")), "タブ")
    assert 'class="tab-more current"' in tabs
    assert re.search(r'<a href="/care" aria-current="page">', tabs)
    assert tabs.count('aria-current="page"') == 1


def test_tab_bar_gives_admins_the_management_section(gui):
    server, _, _ = gui
    tabs = nav_of(page(server, "/manage/users", login(server, "管理者")), "タブ")
    assert re.search(r'<a href="/manage" aria-current="page"><svg class="icon".*?</svg>管理', tabs)
    assert 'class="tab-more current"' in tabs


def test_tab_bar_counts_pending_reviews(gui):
    server, _, _ = gui
    html = page(server, "/", login(server, role="member"))
    in_menu = '<span class="badge">' in nav_of(html, "メインメニュー")
    assert ('<span class="badge">' in nav_of(html, "タブ")) == in_menu


def test_pages_without_login_have_no_tab_bar(gui):
    server, _, _ = gui
    host = {"Host": f"localhost:{server.server_address[1]}"}
    _, _, body = exchange(server, "GET", "/login", headers=host, auth=False)
    assert 'aria-label="タブ"' not in body.decode()


def test_every_menu_icon_exists():
    from poop_detector.gui.icons import ICONS
    from poop_detector.gui.navigation import Viewer, frame_for

    frame = frame_for(Viewer("管理者", True, "/", lambda: "connected", lambda: 2))
    items = (*frame.tabs, *frame.more)
    assert [item.text for item in frame.tabs] == list(TABS.values())
    assert [item.text for item in frame.more] == [
        "お世話",
        "体重",
        "行動マップ",
        "管理",
        "自分の設定",
        "ログアウト",
    ]
    assert all(item.icon in ICONS for item in items)
    assert frame.tabs[1].badge == 2
    assert not frame.more_current
