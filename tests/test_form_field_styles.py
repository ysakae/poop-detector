"""画面で使う入力欄のすべての種類に、共通の枠と色が付くことを検証する（Issue #193）。"""

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "poop_detector" / "gui" / "templates"
# 共通の入力欄の見た目を当てない種類（ボタン、選択肢、隠し項目）。
UNSTYLED = {"hidden", "button", "submit", "checkbox", "radio"}


def field_rule() -> tuple[str, str]:
    """入力欄の共通の見た目（枠・文字色・背景）を決める規則のセレクター。"""
    css = (TEMPLATES / "style.css").read_text(encoding="utf-8")
    for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if "textarea" in selectors and "border:" in body and "background:" in body:
            return selectors, body
    raise AssertionError("入力欄の共通の規則が見つからない")


def test_every_input_type_gets_the_common_field_style():
    used = set()
    for path in TEMPLATES.glob("*.html"):
        used |= set(re.findall(r'<input[^>]*\btype="([a-z-]+)"', path.read_text(encoding="utf-8")))
    selectors, _ = field_rule()
    missing = sorted(kind for kind in used - UNSTYLED if f"input[type={kind}]" not in selectors)
    assert not missing, f"共通の見た目がない入力欄：{missing}"


def test_fields_and_adjacent_buttons_share_a_height():
    _, fields = field_rule()
    css = (TEMPLATES / "style.css").read_text()
    buttons = re.search(r"button,a\.button\{([^}]+)\}", css)
    assert buttons
    assert "min-height:var(--control-height)" in fields
    assert "min-height:var(--control-height)" in buttons[1]


@pytest.mark.parametrize(
    ("template", "action"),
    [
        ("care", "/care"),
        ("records", "/records"),
        ("activity", "/activity"),
        ("storage", "/manage/storage"),
        ("users", "/manage/users"),
        ("account", "/account/name"),
        ("account", "/account/email"),
        ("account", "/account/email/confirm"),
        ("weights", "/weight"),
    ],
)
def test_field_and_button_rows_use_the_shared_layout(template, action):
    html = (TEMPLATES / f"{template}.html").read_text()
    forms = re.findall(rf'<form\b[^>]*action="{re.escape(action)}"[^>]*>', html)
    assert any('class="toolbar field-toolbar"' in form for form in forms)
