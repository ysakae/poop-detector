"""いつもとの違いの画面とホームに出す値（日ごとの移動・睡眠・排泄の回数と、外れた指標）。"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from ..features.daily_trends import DIRECTIONS, METRICS, USUAL_DAYS, DayValues, gather, unusual
from ..timezones import local_today

if TYPE_CHECKING:
    from .server import GuiApp

SHOWN_DAYS = 14


@dataclass(frozen=True)
class Cell:
    """表の1マス。note は、いつもと違うときの「多い」「少ない」。"""

    text: str
    note: str = ""


@dataclass(frozen=True)
class TrendRow:
    """表の1行（1日分）。"""

    day: date
    cells: list[Cell]


def value_text(metric: str, value: float | None) -> str:
    if value is None:
        return "—"
    if metric == "sleep":
        hours, minutes = divmod(round(value), 60)
        return f"{hours}時間{minutes:02d}分"
    return f"{round(value)}{METRICS[metric].unit}"


def _row(days: dict[date, DayValues], day: date) -> TrendRow:
    flags = {item.metric: DIRECTIONS[item.direction] for item in unusual(days, day)}
    cells = [Cell(value_text(m, days[day].get(m)), flags.get(m, "")) for m in METRICS]
    return TrendRow(day, cells)


def trend_rows(app: "GuiApp", last: date) -> list[TrendRow]:
    """last から SHOWN_DAYS 日さかのぼった行を、新しい順に返す。"""
    start = last - timedelta(days=SHOWN_DAYS - 1)
    days = gather(app.root, app.store, start - timedelta(days=USUAL_DAYS), last)
    return [_row(days, last - timedelta(days=i)) for i in range(SHOWN_DAYS)]


def yesterday_notes(app: "GuiApp") -> list[str]:
    """ホームに出す、昨日いつもと違った指標（「睡眠が少ない」など）。"""
    yesterday = local_today() - timedelta(days=1)
    days = gather(app.root, app.store, yesterday - timedelta(days=USUAL_DAYS), yesterday)
    return [item.text for item in unusual(days, yesterday)]
