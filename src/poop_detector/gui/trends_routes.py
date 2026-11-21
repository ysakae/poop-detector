"""いつもとの違い（移動・睡眠・排泄の回数の日ごとの推移）の画面のルート。"""

from datetime import date
from typing import TYPE_CHECKING

from ..features.daily_trends import METRICS, USUAL_DAYS
from ..timezones import local_today
from .pages import Query
from .routes import first, get
from .templating import render_template
from .trends_page import trend_rows

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

TRENDS_PAGE = "/trends"


@get(TRENDS_PAGE)
def trends_page(handler: "Handler", query: Query) -> None:
    """直近の日ごとの値を新しい順に並べ、いつもと違う値に印を付ける。今日は途中の値。"""
    today = local_today()
    try:
        last = date.fromisoformat(first(query, "day")) if first(query, "day") else today
    except ValueError:
        last = today
    handler.respond(
        200,
        render_template(
            "trends.html",
            title=handler.app.title,
            headers=[metric.label for metric in METRICS.values()],
            rows=trend_rows(handler.app, min(last, today)),
            today=today,
            usual_days=USUAL_DAYS,
        ),
    )
