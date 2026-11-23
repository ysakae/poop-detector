"""留守中のまとめの画面に出す値（年表の各行のアイコン・リンク・表示用の時刻）を整える。"""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from ..features.digest_timeline import TimelineEntry, collect
from ..records.away_periods import AwayPeriod
from ..timezones import local_now
from .clip_pages import SOURCES
from .list_time import ListTime, list_time
from .pages import event_url

if TYPE_CHECKING:
    from .server import GuiApp

ICONS = {
    "record": "paw",
    "detection": "paw",
    "clip": "video",
    "absence": "user",
    "vocal": "user",
    "floor": "sheet",
}


@dataclass(frozen=True)
class TimelineItem:
    """年表の1行。icon は gui/icons.py の形の名前。"""

    at: datetime
    when: ListTime | None
    icon: str
    text: str
    url: str


@dataclass(frozen=True)
class Digest:
    """1回の外出のまとめ。asleep_minutes は睡眠の記録があるときだけ。"""

    period: AwayPeriod
    end: datetime
    items: list[TimelineItem]
    asleep_minutes: int | None


def _url(entry: TimelineEntry) -> str:
    if entry.kind in ("record", "detection"):
        return event_url(entry.ref)
    if entry.kind == "clip":
        return "/clips?" + urlencode({"id": entry.ref})
    if entry.kind == "floor":
        return "/floor?" + urlencode({"id": entry.ref})
    return {"absence": "/presence", "vocal": "/vocal"}[entry.kind]


def _item(entry: TimelineEntry) -> TimelineItem:
    return TimelineItem(
        entry.at, list_time(entry.at.isoformat()), ICONS[entry.kind], entry.text, _url(entry)
    )


def build_digest(app: "GuiApp", period: AwayPeriod) -> Digest:
    """外出の間（外出中ならいままで）の出来事を、時刻の順に並べる。"""
    end = period.ended_at or local_now()
    content = collect(app.store, app.clips, app.root, period.started_at, end, SOURCES)
    return Digest(period, end, [_item(e) for e in content.entries], content.asleep_minutes)
