"""押したことがすぐ分かる表示（送信中のボタンと、画面の移動中の帯）を検証する。"""

import re
from pathlib import Path

from support import exchange, login

STATIC = Path(__file__).resolve().parents[1] / "src" / "poop_detector" / "gui" / "static"
TEMPLATES = STATIC.parent / "templates"


def page(server, path, cookie):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def test_every_signed_in_page_loads_the_busy_script(gui):
    server, _, _ = gui
    cookie = login(server)
    for path in ("/", "/records", "/manage", "/manage/models"):
        assert '<script src="/assets/busy.js" defer></script>' in page(server, path, cookie)
    status, headers, _ = exchange(server, "GET", "/assets/busy.js")
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")


def test_script_leaves_handled_submits_and_clicks_alone():
    script = (STATIC / "busy.js").read_text("utf-8")
    # ほかのスクリプトが自分で処理した送信やクリックには、表示を出さない。
    assert script.count("defaultPrevented") >= 2
    # 戻るボタンで画面が戻ったときは、表示を消す。
    assert "pageshow" in script
    # ダウンロードのように画面が変わらない送信は、印を付けて除く。
    assert "data-no-busy" in script
    # 送信の値が消えないよう、ボタンを止めるのは送信を始めたあと。
    assert "setTimeout" in script


def test_downloads_do_not_show_the_busy_state(gui):
    server, _, _ = gui
    html = page(server, "/manage/export", login(server))
    for action in ("/manage/export/records.csv", "/manage/export/backup"):
        form = re.search(rf'<form[^>]*action="{action}"[^>]*>', html)[0]
        assert "data-no-busy" in form


def test_slow_buttons_say_what_they_are_doing():
    html = (TEMPLATES / "models.html").read_text("utf-8")
    assert 'data-busy-label="CLI に問い合わせています…"' in html


def test_busy_styles_respect_reduced_motion():
    css = (TEMPLATES / "style.css").read_text("utf-8")
    assert ".page-busy" in css
    assert 'button[aria-busy="true"]' in css
    assert re.search(r"prefers-reduced-motion:\s*reduce\)[^}]*\.page-busy", css)
