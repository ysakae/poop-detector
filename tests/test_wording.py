"""画面の言葉が docs/screens.md の「画面の言葉」の表にそろっていることを検証する（Issue #191）。"""

import re
from pathlib import Path

import pytest
from support import request

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "poop_detector" / "gui" / "templates"
GUI = ROOT / "src" / "poop_detector" / "gui"

# 使わない言い換え → 使う言葉。docs/screens.md の表と同じ内容にする。
AVOIDED = {
    "イベント": "検知",
    "確認済みの排泄記録": "確定記録",
    "確定した記録": "確定記録",
    "確定した排泄": "確定記録",
    "動画ラベルのみ保存": "ラベルを保存",
    "手動で記録する": "排泄を記録",
}


def visible_template_text(path: Path) -> str:
    """テンプレートから Jinja のコメントを除いた、画面に出うる部分。"""
    return re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.S)


def visible_python_strings(path: Path) -> str:
    """Python のモジュールから、コメントと docstring を除いた文字列リテラル。"""
    source = path.read_text(encoding="utf-8")
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    lines = [line.split("#", 1)[0] for line in source.splitlines()]
    return "\n".join(re.findall(r'"[^"\n]*"', "\n".join(lines)))


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_templates_use_the_glossary(path):
    text = visible_template_text(path)
    for avoided, preferred in AVOIDED.items():
        assert avoided not in text, f"{path.name}：「{avoided}」ではなく「{preferred}」を使う"


@pytest.mark.parametrize("path", sorted(GUI.glob("*.py")), ids=lambda p: p.name)
def test_gui_strings_use_the_glossary(path):
    text = visible_python_strings(path)
    for avoided, preferred in AVOIDED.items():
        assert avoided not in text, f"{path.name}：「{avoided}」ではなく「{preferred}」を使う"


def test_glossary_is_documented():
    screens = (ROOT / "docs" / "screens.md").read_text(encoding="utf-8")
    section = screens.split("## 画面の言葉", 1)[1].split("\n## ", 1)[0]
    for avoided, preferred in AVOIDED.items():
        assert avoided in section
        assert preferred in section


def test_detections_are_found_among_the_clips(gui):
    """検知は動画の一覧の「差分検知」で探し、1件は「記録の詳細」で開く。"""
    server, _, event = gui
    status, body = request(server, "GET", "/clips?show=detected")
    assert status == 200
    assert ">差分検知 " in body.decode()
    status, body = request(server, "GET", f"/records/events?id={event}")
    assert status == 200
    assert "<h2>記録の詳細</h2>" in body.decode()
