"""夜間のまとめ（夜ごとの睡眠の数字・年表・タイムラプス）の画面。"""

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from ..config import Config
from ..features.daily_trends import DIRECTIONS
from ..features.digest import DigestSnapshots
from ..features.digest_timeline import collect
from ..features.night import (
    latest_night,
    length_text,
    night_id,
    night_records,
    night_window,
    sleep_deviation,
    sleep_summary,
)
from ..timezones import local_now
from .clip_pages import SOURCES
from .digest_page import TimelineItem, timeline_item
from .list_time import list_time
from .pages import Query
from .routes import first, get
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler
    from .server import GuiApp

NIGHT_PAGE = "/night"
SHOWN_NIGHTS = 14


def night_config(app: "GuiApp") -> Config:
    return Config.load(app.config_path) if app.config_path is not None else Config()


def home_night(app: "GuiApp") -> date | None:
    """ホームに入口を出す昨夜（夜間のまとめを使わない設定なら None）。"""
    config = night_config(app)
    if not config.night_enabled:
        return None
    return latest_night(local_now(), config.night_hours)


def _timeline(app: "GuiApp", night: date, hours: str) -> list[TimelineItem]:
    start, end = night_window(night, hours)
    content = collect(app.store, app.clips, app.root, start, end, SOURCES)
    items = [timeline_item(entry) for entry in content.entries]
    summary = sleep_summary(night_records(app.store, night, hours))
    for begin, finish in summary.awake_spans:
        minutes = round((finish - begin).total_seconds() / 60)
        items.append(
            TimelineItem(
                begin, list_time(begin.isoformat()), "user", f"起きていた（{minutes}分）", "/sleep"
            )
        )
    items.sort(key=lambda item: item.at)
    return items


def _detail(app: "GuiApp", night: date, hours: str) -> dict[str, Any]:
    start, end = night_window(night, hours)
    summary = sleep_summary(night_records(app.store, night, hours))
    unusual = sleep_deviation(app.store, night, hours)
    return {
        "night": night,
        "id": night_id(night),
        "start": list_time(start.isoformat()),
        "end": list_time(end.isoformat()),
        "observed": summary.observed_minutes > 0,
        "asleep": length_text(summary.asleep_minutes),
        "longest": length_text(summary.longest_minutes),
        "awakenings": summary.awakenings,
        "unusual": f"いつもより{DIRECTIONS[unusual.direction]}" if unusual else "",
        "items": _timeline(app, night, hours),
        "frames": len(DigestSnapshots(app.root).frames(night_id(night))),
    }


@get(NIGHT_PAGE)
def night_page(handler: "Handler", query: Query) -> None:
    """?day= があればその夜のまとめ、なければ直近の夜の一覧を出す。"""
    app = handler.app
    config = night_config(app)
    hours = config.night_hours
    text = first(query, "day")
    if text:
        try:
            night = date.fromisoformat(text)
        except ValueError:
            handler.respond(404, "夜間のまとめが見つかりません")
            return
        page = render_template(
            "night.html",
            title=app.title,
            enabled=config.night_enabled,
            **_detail(app, night, hours),
        )
        handler.respond(200, page)
        return
    last = latest_night(local_now(), hours)
    nights = []
    for offset in range(SHOWN_NIGHTS):
        night = last - timedelta(days=offset)
        summary = sleep_summary(night_records(app.store, night, hours))
        nights.append(
            {
                "night": night,
                "asleep": length_text(summary.asleep_minutes) if summary.observed_minutes else "—",
                "awakenings": summary.awakenings if summary.observed_minutes else None,
            }
        )
    page = render_template(
        "night_list.html", title=app.title, nights=nights, enabled=config.night_enabled
    )
    handler.respond(200, page)
