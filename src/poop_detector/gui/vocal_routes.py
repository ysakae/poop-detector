"""鳴き声の記録（日ごとの鳴いていた時間と、最近の区間）の画面。"""

from datetime import timedelta
from typing import TYPE_CHECKING

from ..records.vocal_episodes import VocalEpisodes
from ..timezones import local_today
from .list_time import list_time
from .pages import Query
from .routes import get
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

VOCAL_PAGE = "/vocal"
SHOWN_DAYS = 14


def loud_text(seconds: float) -> str:
    """鳴いていた時間の短い表示（1分未満は秒）。"""
    if seconds < 60:
        return f"{round(seconds)}秒"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}分{rest:02d}秒"


@get(VOCAL_PAGE)
def vocal_page(handler: "Handler", _query: Query) -> None:
    """直近の日ごとの鳴いていた時間と、最近の区間を新しい順に出す。"""
    episodes = VocalEpisodes(handler.app.store.database)
    today = local_today()
    first = today - timedelta(days=SHOWN_DAYS - 1)
    totals = episodes.daily_totals(first, today)
    days = [
        {
            "day": day,
            "count": totals.get(day, (0, 0.0))[0],
            "loud": loud_text(totals.get(day, (0, 0.0))[1]),
        }
        for day in (today - timedelta(days=i) for i in range(SHOWN_DAYS))
    ]
    recent = [
        {
            "when": list_time(item.started_at.isoformat()),
            "loud": loud_text(item.loud_seconds),
            "peak": f"{item.peak_db:.0f}",
        }
        for item in episodes.list(30)
    ]
    handler.respond(
        200,
        render_template(
            "vocal.html", title=handler.app.title, days=days, recent=recent, today=today
        ),
    )
