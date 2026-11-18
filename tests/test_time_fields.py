"""画面の日時の入力欄が、秒を含まない分までの入力であることを検証する。"""

import re
from pathlib import Path

from poop_detector.gui.pages import local_time

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "poop_detector" / "gui" / "templates"


def test_no_time_field_asks_for_seconds():
    fields = []
    for path in TEMPLATES.glob("*.html"):
        fields += re.findall(r'<input[^>]*type="datetime-local"[^>]*>', path.read_text("utf-8"))
    assert fields
    assert [field for field in fields if "step=" in field] == []


def test_form_values_are_to_the_minute():
    assert local_time("2024-03-01T08:05:42+09:00", form=True) == "2024-03-01T08:05"
    # 画面に出すだけの時刻は、これまでどおり秒まで示す。
    assert local_time("2024-03-01T08:05:42+09:00") == "2024/03/01 08:05:42"
