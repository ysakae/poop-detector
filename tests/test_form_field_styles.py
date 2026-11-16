"""画面で使う入力欄のすべての種類に、共通の枠と色が付くことを検証する（Issue #193）。"""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "poop_detector" / "gui" / "templates"
# 共通の入力欄の見た目を当てない種類（ボタン、選択肢、隠し項目）。
UNSTYLED = {"hidden", "button", "submit", "checkbox", "radio"}


def field_rule() -> str:
    """入力欄の共通の見た目（枠・文字色・背景）を決める規則のセレクター。"""
    css = (TEMPLATES / "style.css").read_text(encoding="utf-8")
    for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if "textarea" in selectors and "border:" in body and "background:" in body:
            return selectors
    raise AssertionError("入力欄の共通の規則が見つからない")


def test_every_input_type_gets_the_common_field_style():
    used = set()
    for path in TEMPLATES.glob("*.html"):
        used |= set(re.findall(r'<input[^>]*\btype="([a-z-]+)"', path.read_text(encoding="utf-8")))
    selectors = field_rule()
    missing = sorted(kind for kind in used - UNSTYLED if f"input[type={kind}]" not in selectors)
    assert not missing, f"共通の見た目がない入力欄：{missing}"
