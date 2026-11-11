"""GUI の HTML テンプレート（Jinja2）。

値はすべて自動でエスケープする（autoescape）。テンプレートにない変数を使うと
StrictUndefined がエラーにするので、渡し忘れが空欄として紛れ込まない。
"""

from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

ENVIRONMENT = Environment(
    loader=PackageLoader("poop_detector.gui", "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_template(name: str, **context: Any) -> str:
    """templates/ のテンプレートを描画する。"""
    return ENVIRONMENT.get_template(name).render(**context)
