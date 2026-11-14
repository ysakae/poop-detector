"""デザインの変数と表示の選択（#87、#141）を検証する。

色はデザイントークン（CSS のカスタムプロパティ）にまとめ、画面の CSS はトークンだけを参照する。
デザインは1つで、トークンは light-dark(ライト, ダーク) で書くので、ライトとダークの両方で、文字と背景のコントラストが WCAG 2.2 の AA（4.5:1、大きな文字と部品は 3:1）以上かを確かめる。
"""

import re
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

import pytest
from support import exchange, login

from poop_detector.config import Config, ConfigError
from poop_detector.gui.accounts import Accounts
from poop_detector.gui.templating import ENVIRONMENT
from poop_detector.gui.themes import APPEARANCES
from poop_detector.records.database import events_database

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
    ("chip-ok-text", "chip-ok-bg", 4.5),
    ("chip-warn-text", "chip-warn-bg", 4.5),
    ("chip-off-text", "chip-off-bg", 4.5),
    ("ok", "surface", 3),
    ("warn", "surface", 3),
    ("off", "surface", 3),
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
def test_design_meets_wcag_aa_contrast(mode):
    values = tokens(token_block(":root"), mode)
    failures = [
        f"{fg} / {bg}: {contrast(values[fg], values[bg]):.2f}"
        for fg, bg, needed in PAIRS
        if contrast(values[fg], values[bg]) < needed
    ]
    assert not failures, f"{mode} のコントラストが足りません: {failures}"


def test_there_is_one_design():
    """テーマごとの変数の上書きを置かない（#141）。"""
    assert "data-theme" not in STYLE
    assert "data-theme" not in (TEMPLATES / "base.html").read_text()


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


def appearance_attribute(html):
    match = re.search(r'<html lang="ja" data-appearance="([a-z]+)">', html)
    assert match, "html 要素に明るさがありません"
    return match[1]


def page(server, cookie, path="/"):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def test_default_follows_the_os(gui):
    server, _, _ = gui
    assert appearance_attribute(page(server, login(server, role="member"))) == "auto"


def test_users_choose_their_own_appearance(gui):
    server, _, _ = gui
    cookie = login(server, "家族B", role="member")
    html = page(server, cookie, "/account")
    assert "明るさ" in html
    assert 'name="theme"' not in html
    token = re.search(r'name="csrf" value="([^"]+)"', html)[1]
    body = urlencode({"appearance": "dark", "csrf": token})
    status, headers, _ = exchange(
        server, "POST", "/account/display", body, {**FORM, **cookie}, auth=False
    )
    assert (status, headers["Location"]) == (303, "/account?display_saved=1")
    assert appearance_attribute(page(server, cookie)) == "dark"
    # ほかの人の表示は変わらない。
    assert appearance_attribute(page(server, login(server, role="member"))) == "auto"
    assert server.app.accounts.users.by_name("家族B").appearance == "dark"


def test_invalid_appearance_is_refused(gui):
    server, _, _ = gui
    cookie = login(server, role="member")
    token = re.search(r'name="csrf" value="([^"]+)"', page(server, cookie, "/account"))[1]
    body = urlencode({"appearance": "neon", "csrf": token})
    status, _, _ = exchange(
        server, "POST", "/account/display", body, {**FORM, **cookie}, auth=False
    )
    assert status == 400
    assert appearance_attribute(page(server, cookie)) == "auto"


def test_old_theme_setting_is_still_accepted():
    """デザインを1つにしたあとも、gui.theme を書いた設定ファイルで起動できる。"""
    assert Config(gui_theme="dog").gui_theme == "dog"
    with pytest.raises(ConfigError, match=r"gui\.theme"):
        Config(gui_theme="neon")


def test_appearance_choices_are_listed():
    assert set(APPEARANCES) == {"auto", "light", "dark"}


def test_v7_database_keeps_users_and_their_old_theme(tmp_path):
    """theme 列は消さない。選んであった値は残し、明るさだけを読み書きする。"""
    from poop_detector.records.database import MIGRATIONS

    with sqlite3.connect(tmp_path / "events.sqlite3") as db:
        for migration in MIGRATIONS[:7]:
            migration(db)
        db.execute("PRAGMA user_version = 7")
        db.execute(
            "INSERT INTO users (id, name, role, created_at) VALUES ('old', '家族A', 'admin', '2026')"
        )
    database = events_database(tmp_path)
    users = Accounts(database).users
    with database.connect() as db:
        db.execute("UPDATE users SET theme = 'dog' WHERE id = 'old'")
    assert (users.get("old").name, users.get("old").appearance) == ("家族A", "auto")
    users.set_appearance("old", "light")
    assert users.get("old").appearance == "light"
    with database.connect() as db:
        assert db.execute("SELECT theme FROM users WHERE id = 'old'").fetchone()[0] == "dog"


@pytest.mark.parametrize("name", ["paw.svg", "dog.svg"])
def test_illustrations_are_served_from_this_server(gui, name):
    """CSP の img-src 'self' のため、絵は data: URI でなく同梱の SVG を読む。"""
    server, _, _ = gui
    assert "data:image" not in STYLE
    status, headers, body = exchange(server, "GET", f"/assets/{name}", auth=False)
    assert status == 200
    assert headers["Content-Type"] == "image/svg+xml"
    assert b"<script" not in body


def macros():
    return ENVIRONMENT.get_template("_macros.html").module


def test_every_icon_draws_with_the_text_color():
    from poop_detector.gui.icons import ICONS

    assert {"home", "review", "records", "video", "feces", "urine", "food", "sheet"} <= set(ICONS)
    for name in ICONS:
        svg = str(macros().icon(name))
        assert svg.startswith('<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"'), name
        assert "currentColor" not in svg, name
        assert "#" not in svg, name
        assert "<path" in svg, name


def test_unknown_icon_is_an_error():
    with pytest.raises(Exception, match="nonexistent"):
        macros().icon("nonexistent")


def test_components_escape_their_values():
    module = macros()
    assert "&lt;b&gt;" in str(module.stat("排便", "<b>", "回", "feces"))
    assert "&lt;b&gt;" in str(module.chip("<b>", "ok"))
    empty = str(module.empty_state("<b>", "説明"))
    assert "&lt;b&gt;" in empty
    assert 'src="/assets/dog.svg"' in empty


def test_component_styles_exist():
    for selector in [".icon{", ".stat{", ".chip{", ".chip-ok{", ".empty-state{", ".ghost{"]:
        assert selector in STYLE, selector
