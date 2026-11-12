"""GUI の HTML テンプレート（Jinja2）。

値はすべて自動でエスケープする（autoescape）。テンプレートにない変数を使うと
StrictUndefined がエラーにするので、渡し忘れが空欄として紛れ込まない。
"""

from contextvars import ContextVar
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

from .navigation import Viewer, frame_for

ENVIRONMENT = Environment(
    loader=PackageLoader("poop_detector.gui", "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


# 画面を見ている人。asgi.dispatch がルートを呼ぶスレッドで設定する（ログインしていなければ None）。
# テンプレートは frame で共通の枠を、viewer_admin で管理者だけの操作の入口を出し分ける
# （拒否はサーバーで行う）。
VIEWER: ContextVar[Viewer | None] = ContextVar("viewer", default=None)


def render_template(name: str, **context: Any) -> str:
    """templates/ のテンプレートを描画する。"""
    viewer = VIEWER.get()
    return ENVIRONMENT.get_template(name).render(
        viewer_admin=viewer is not None and viewer.admin, frame=frame_for(viewer), **context
    )
