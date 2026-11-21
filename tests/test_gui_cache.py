"""ブラウザーのキャッシュの回帰テスト。

セルラー回線で画面を移るたびにスクリプト・スタイル・画像を取り直さないよう、
同梱の静的ファイルと画像だけをブラウザーに預け、画面（HTML）と JSON は預けない。
"""

import re
from urllib.parse import urlencode

from support import csrf_token, exchange
from test_clip_thumbnails import playable_clip

from poop_detector.gui.asgi import SECURITY_HEADERS, is_public
from poop_detector.gui.assets import IMAGE_CACHE, IMMUTABLE, REVALIDATE, asset_url
from poop_detector.gui.routes import GET_ROUTES
from poop_detector.video.clips import ClipStore

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def test_pages_link_the_stylesheet_instead_of_inlining_it(gui):
    server, _, _ = gui
    status, headers, body = exchange(server, "GET", "/")
    html = body.decode()

    assert status == 200
    assert f'<link rel="stylesheet" href="{asset_url("style.css")}">' in html
    assert "<style>" not in html
    # 画面そのものは端末に残さない。
    assert headers["Cache-Control"] == "no-store"


def test_style_policy_allows_the_linked_stylesheet():
    assert "style-src 'self' 'unsafe-inline'" in SECURITY_HEADERS["Content-Security-Policy"]


def test_stylesheet_opens_before_login():
    """ログインの画面にも同じスタイルを使う。"""
    assert is_public("/assets/style.css")


def test_every_versioned_url_in_pages_is_a_route(gui):
    server, _, _ = gui
    html = exchange(server, "GET", "/")[2].decode()
    urls = re.findall(r'(/assets/[^"?]+)\?v=[0-9a-f]+', html)

    assert "/assets/style.css" in urls
    assert "/assets/menu.js" in urls
    assert all(url in GET_ROUTES for url in urls)


def test_versioned_asset_is_kept_by_the_browser(gui):
    server, _, _ = gui
    status, headers, body = exchange(server, "GET", asset_url("menu.js"))

    assert status == 200
    assert headers["Cache-Control"] == IMMUTABLE
    assert "private" in IMMUTABLE
    assert headers["ETag"]
    assert body


def test_stylesheet_is_served_as_css(gui):
    server, _, _ = gui
    status, headers, body = exchange(server, "GET", asset_url("style.css"), auth=False)

    assert status == 200
    assert headers["Content-Type"].startswith("text/css")
    assert headers["Cache-Control"] == IMMUTABLE
    assert b".site-header" in body


def test_unversioned_or_old_asset_is_revalidated(gui):
    """版のない URL（CSS の中の画像、マニフェストのアイコン）や古い版は、確かめてから使う。"""
    server, _, _ = gui
    for path in ("/assets/menu.js", "/assets/menu.js?v=0000", "/assets/paw.svg"):
        status, headers, _ = exchange(server, "GET", path)
        assert status == 200
        assert headers["Cache-Control"] == REVALIDATE


def test_unchanged_asset_answers_not_modified(gui):
    server, _, _ = gui
    tag = exchange(server, "GET", "/assets/dog.svg")[1]["ETag"]

    status, headers, body = exchange(
        server, "GET", "/assets/dog.svg", headers={"If-None-Match": tag}
    )

    assert status == 304
    assert body == b""
    assert headers["ETag"] == tag


def test_icons_are_revalidated(gui):
    server, _, _ = gui
    status, headers, _ = exchange(server, "GET", "/assets/icon-192.png", auth=False)

    assert status == 200
    assert headers["Cache-Control"] == REVALIDATE
    assert headers["ETag"]


def test_thumbnails_and_event_images_are_kept_for_a_while(gui):
    server, store, event = gui
    clip_id, _ = playable_clip(ClipStore(store.root))

    for path in (f"/clip-thumbnail?id={clip_id}", f"/image?id={event}"):
        status, headers, _ = exchange(server, "GET", path)
        assert status == 200
        assert headers["Cache-Control"] == IMAGE_CACHE
    assert "private" in IMAGE_CACHE


def test_missing_images_are_not_kept(gui):
    server, _, _ = gui
    for path in ("/clip-thumbnail?id=unknown", "/image?id=unknown"):
        status, headers, _ = exchange(server, "GET", path)
        assert status == 404
        assert headers["Cache-Control"] == "no-store"


def test_logout_clears_the_browser_cache(gui):
    server, _, _ = gui
    body = urlencode({"csrf": csrf_token(server)})
    status, headers, _ = exchange(server, "POST", "/logout", body, FORM)

    assert status == 200
    assert headers["Clear-Site-Data"] == '"cache"'
