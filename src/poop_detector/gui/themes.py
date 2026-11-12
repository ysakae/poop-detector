"""デザインテーマ。テーマ（配色・形）と、ライト・ダークの表示を、人ごとに選べる。

色と形は templates/style.css のデザイントークンにあり、html 要素の data-theme と data-appearance で
切り替える。テーマを選んでいない人は、管理者が設定（gui.theme）で決めた既定のテーマを使う。
"""

from dataclasses import dataclass

from ..config import THEME_NAMES

THEMES: dict[str, str] = {"professional": "プロフェッショナル", "dog": "ワンちゃん"}
APPEARANCES: dict[str, str] = {"auto": "OS の設定に合わせる", "light": "ライト", "dark": "ダーク"}
DEFAULT_THEME = "professional"
DEFAULT_APPEARANCE = "auto"
assert tuple(THEMES) == THEME_NAMES  # noqa: S101  # 設定の検証と画面の選択肢をそろえる


@dataclass(frozen=True)
class Display:
    """画面の表示。theme は THEMES、appearance は APPEARANCES のどれか。"""

    theme: str = DEFAULT_THEME
    appearance: str = DEFAULT_APPEARANCE


def resolve(theme: str | None, appearance: str | None, default_theme: str) -> Display:
    """人の選択（未選択は None）と既定のテーマから、画面に使う表示を決める。"""
    chosen = theme if theme in THEMES else default_theme
    return Display(
        chosen if chosen in THEMES else DEFAULT_THEME,
        appearance if appearance in APPEARANCES else DEFAULT_APPEARANCE,
    )
