"""画面の表示。デザインは1つで、ライト・ダークだけを人ごとに選べる（#141）。

色と形は templates/style.css のデザインの変数にあり、html 要素の data-appearance で切り替える。
"""

from dataclasses import dataclass

APPEARANCES: dict[str, str] = {"auto": "OS の設定に合わせる", "light": "ライト", "dark": "ダーク"}
DEFAULT_APPEARANCE = "auto"


@dataclass(frozen=True)
class Display:
    """画面の表示。appearance は APPEARANCES のどれか。"""

    appearance: str = DEFAULT_APPEARANCE


def resolve(appearance: str | None) -> Display:
    """人の選択（未選択は None）から、画面に使う表示を決める。"""
    return Display(appearance if appearance in APPEARANCES else DEFAULT_APPEARANCE)
