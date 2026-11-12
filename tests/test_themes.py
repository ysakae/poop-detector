"""デザインテーマ（#87）を検証する。

色はデザイントークン（CSS のカスタムプロパティ）にまとめ、画面の CSS はトークンだけを参照する。
トークンはテーマごとに light-dark(ライト, ダーク) で書くので、両テーマ・ライトとダークの組み合わせ
すべてで、文字と背景のコントラストが WCAG 2.2 の AA（4.5:1、大きな文字と部品は 3:1）以上かを確かめる。
"""

import re
import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest
from support import exchange, login

from poop_detector.config import Config, ConfigError
from poop_detector.gui.accounts import Accounts
from poop_detector.gui.themes import APPEARANCES, THEMES
from poop_detector.records.database import events_database
from poop_detector.web_address import WebOptions

TEMPLATES = Path(__file__).parents[1] / "src" / "poop_detector" / "gui" / "templates"
STYLE = (TEMPLATES / "style.css").read_text()
FORM = {"Content-Type": "application/x-www-form-urlencoded"}

# (前景, 背景, 必要な比)。本文の文字は 4.5、大きな文字・アイコン・部品の境界は 3。
PAIRS = [
    ("text", "bg", 4.5),
    ("text", "surface", 4.5),
    ("text", "surface-2", 4.5),
    ("muted", "bg", 4.5),
    ("muted", "surface", 4.5),
    ("link", "surface", 4.5),
    ("link", "bg", 4.5),
    ("on-primary", "primary", 4.5),
    ("primary", "surface", 3),
    ("header-text", "header-bg", 4.5),
    ("menu-text", "menu-bg", 4.5),
    ("menu-current", "menu-bg", 4.5),
    ("text", "success-bg", 4.5),
    ("warn-text", "warn-bg", 4.5),
    ("text", "error-bg", 4.5),
    ("badge-text", "badge-bg", 4.5),
    ("media-text", "media-bg", 4.5),
    ("border-strong", "surface", 3),
    ("focus", "surface", 3),
    ("focus", "bg", 3),
]


def token_block(selector):
    match = re.search(re.escape(selector) + r"\{(.*?)\}", STYLE, re.S)
    assert match, f"{selector} のトークンがありません"
    return match[1]


def tokens(block, mode):
    """ブロックの色のトークンを、mode（light か dark）の値の辞書にする。"""
    values = {}
    for name, value in re.findall(r"--([a-z0-9-]+):\s*([^;]+);", block):
        pair = re.fullmatch(
            r"light-dark\(\s*(#[0-9a-f]{6})\s*,\s*(#[0-9a-f]{6})\s*\)", value.strip()
        )
        if pair:
            values[name] = pair[1] if mode == "light" else pair[2]
        elif re.fullmatch(r"#[0-9a-f]{6}", value.strip()):
            values[name] = value.strip()
    return values


def luminance(color):
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("theme", sorted(THEMES))
def test_every_theme_meets_wcag_aa_contrast(theme, mode):
    base = tokens(token_block(":root"), mode)
    values = (
        base
        if theme == "professional"
        else {
            **base,
            **tokens(token_block(':root[data-theme="dog"]'), mode),
        }
    )
    failures = [
        f"{fg} / {bg}: {contrast(values[fg], values[bg]):.2f}"
        for fg, bg, needed in PAIRS
        if contrast(values[fg], values[bg]) < needed
    ]
    assert not failures, f"{theme}・{mode} のコントラストが足りません: {failures}"


def test_styles_use_only_tokens_for_colors():
    """トークンの定義の外では、色を直接書かない。"""
    outside = re.sub(r":root(\[[^\]]*\])?\{.*?\}", "", STYLE, flags=re.S)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", outside)


def test_templates_do_not_write_colors_inline():
    for path in TEMPLATES.glob("*.html"):
        for style in re.findall(r'style="([^"]*)"', path.read_text()):
            assert not re.search(r"#[0-9a-fA-F]{3,6}|rgba?\(|\bwhite\b|background", style), (
                path.name
            )


def test_no_external_fonts_or_icons():
    """フォントやアイコンを外部の CDN から読まない（CSP と、外部に通信しない方針のため）。"""
    assert "url(http" not in STYLE
    assert "@import" not in STYLE
    for path in TEMPLATES.glob("*.html"):
        assert "fonts.googleapis" not in path.read_text()


def theme_attributes(html):
    match = re.search(r'<html lang="ja" data-theme="([a-z]+)" data-appearance="([a-z]+)">', html)
    assert match, "html 要素にテーマがありません"
    return match[1], match[2]


def page(server, cookie, path="/"):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def test_default_is_professional_following_the_os(gui):
    server, _, _ = gui
    assert theme_attributes(page(server, login(server, role="member"))) == ("professional", "auto")


def test_users_choose_their_own_theme_and_appearance(gui):
    server, _, _ = gui
    cookie = login(server, "家族B", role="member")
    html = page(server, cookie, "/account")
    assert "表示" in html
    token = re.search(r'name="csrf" value="([^"]+)"', html)[1]
    body = urlencode({"theme": "dog", "appearance": "dark", "csrf": token})
    status, headers, _ = exchange(
        server, "POST", "/account/display", body, {**FORM, **cookie}, auth=False
    )
    assert (status, headers["Location"]) == (303, "/account?display_saved=1")
    assert theme_attributes(page(server, cookie)) == ("dog", "dark")
    # ほかの人の表示は変わらない。
    assert theme_attributes(page(server, login(server, role="member"))) == ("professional", "auto")
    user = server.app.accounts.users.by_name("家族B")
    assert (user.theme, user.appearance) == ("dog", "dark")


def test_invalid_display_choices_are_refused(gui):
    server, _, _ = gui
    cookie = login(server, role="member")
    token = re.search(r'name="csrf" value="([^"]+)"', page(server, cookie, "/account"))[1]
    body = urlencode({"theme": "neon", "appearance": "dark", "csrf": token})
    status, _, _ = exchange(
        server, "POST", "/account/display", body, {**FORM, **cookie}, auth=False
    )
    assert status == 400
    assert theme_attributes(page(server, cookie)) == ("professional", "auto")


@pytest.fixture
def dog_default_gui(tmp_path):
    from poop_detector.gui.server import make_server

    server = make_server(tmp_path, 0, web=WebOptions(theme="dog"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def test_admin_default_theme_applies_to_users_who_follow_it(dog_default_gui):
    server = dog_default_gui
    host = {"Host": f"localhost:{server.server_address[1]}"}
    status, _, body = exchange(server, "GET", "/login", headers=host, auth=False)
    assert status == 200
    assert theme_attributes(body.decode())[0] == "dog"
    follower = login(server, role="member")
    assert theme_attributes(page(server, follower))[0] == "dog"
    chooser = login(server, "家族C", role="member")
    users = server.app.accounts.users
    users.set_display(users.by_name("家族C").id, "professional", "light")
    assert theme_attributes(page(server, chooser)) == ("professional", "light")


def test_theme_setting_is_validated():
    assert Config(gui_theme="dog").gui_theme == "dog"
    with pytest.raises(ConfigError, match=r"gui\.theme"):
        Config(gui_theme="neon")
    assert WebOptions.from_config(Config(gui_theme="dog")).theme == "dog"


def test_display_choices_are_listed():
    assert set(THEMES) == {"professional", "dog"}
    assert set(APPEARANCES) == {"auto", "light", "dark"}


def test_v7_database_keeps_users_who_follow_the_default(tmp_path):
    from poop_detector.records.database import MIGRATIONS

    with sqlite3.connect(tmp_path / "events.sqlite3") as db:
        for migration in MIGRATIONS[:7]:
            migration(db)
        db.execute("PRAGMA user_version = 7")
        db.execute(
            "INSERT INTO users (id, name, role, created_at) VALUES ('old', '家族A', 'admin', '2026')"
        )
    users = Accounts(events_database(tmp_path)).users
    user = users.get("old")
    assert (user.name, user.theme, user.appearance) == ("家族A", None, "auto")
    users.set_display("old", None, "light")
    assert (users.get("old").theme, users.get("old").appearance) == (None, "light")


def test_paw_motif_is_served_from_this_server(gui):
    """CSP の img-src 'self' のため、模様は data: URI でなく同梱の SVG を読む。"""
    server, _, _ = gui
    assert 'url("/assets/paw.svg")' in STYLE
    assert "data:image" not in STYLE
    status, headers, body = exchange(server, "GET", "/assets/paw.svg", auth=False)
    assert status == 200
    assert headers["Content-Type"] == "image/svg+xml"
    assert b"<script" not in body
