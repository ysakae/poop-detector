"""留守中のまとめ（1回の外出の間の出来事の年表）を、台帳から集める。"""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from ..features.sleep_store import SleepStore
from ..records.absences import Absences
from ..records.away_periods import AwayPeriod
from ..records.events import EXCRETION_TYPES
from ..records.floor_objects import FloorObjects
from ..timezones import local_now
from .clip_pages import SOURCES
from .list_time import ListTime, list_time
from .pages import event_url

if TYPE_CHECKING:
    from .server import GuiApp


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


def _within(stamp: str | None, start: datetime, end: datetime) -> datetime | None:
    if not stamp:
        return None
    at = datetime.fromisoformat(stamp)
    return at if start <= at < end else None


def _item(at: datetime, icon: str, text: str, url: str) -> TimelineItem:
    return TimelineItem(at, list_time(at.isoformat()), icon, text, url)


def _events(app: "GuiApp", start: datetime, end: datetime) -> list[TimelineItem]:
    items = []
    for row in app.store.list():
        at = _within(row["actual_start"] or row["timestamp"], start, end)
        if at is None:
            continue
        if row["feedback"] == 1 and row["excretion_type"] in EXCRETION_TYPES:
            text = f"{EXCRETION_TYPES[row['excretion_type']]}の記録"
            items.append(_item(at, "paw", text, event_url(row["id"])))
        elif row["kind"] == "SUSPECTED":
            items.append(_item(at, "paw", "トイレの変化を検知", event_url(row["id"])))
    return items


def _clips(app: "GuiApp", start: datetime, end: datetime) -> list[TimelineItem]:
    items = []
    for row in app.clips.list():
        at = _within(row["requested_at"], start, end)
        if at is None or row["status"] != "ready":
            continue
        text = f"動画（{SOURCES.get(row['source'], row['source'])}）"
        items.append(_item(at, "video", text, "/clips?" + urlencode({"id": row["id"]})))
    return items


def _others(app: "GuiApp", start: datetime, end: datetime) -> list[TimelineItem]:
    items = []
    for absence in Absences(app.store.database).list(200):
        if start <= absence.started_at < end:
            length = (
                f"約{round(absence.minutes)}分" if absence.minutes is not None else "続いている"
            )
            items.append(
                _item(absence.started_at, "user", f"サークルにいない（{length}）", "/presence")
            )
    for found in FloorObjects(app.store.database, app.root).list(200):
        at = _within(found.detected_at, start, end)
        if at is not None:
            url = "/floor?" + urlencode({"id": found.id})
            items.append(_item(at, "sheet", "床に見慣れない物", url))
    return items


def _asleep(app: "GuiApp", start: datetime, end: datetime) -> int | None:
    records = SleepStore(app.store.database).minutes(start, end)
    observed = [record for record in records if record.state != "unknown"]
    if not observed:
        return None
    return sum(record.state == "asleep" for record in observed)


def build_digest(app: "GuiApp", period: AwayPeriod) -> Digest:
    """外出の間（外出中ならいままで）の出来事を、時刻の順に並べる。"""
    end = period.ended_at or local_now()
    start = period.started_at
    items = _events(app, start, end) + _clips(app, start, end) + _others(app, start, end)
    items.sort(key=lambda item: item.at)
    return Digest(period, end, items, _asleep(app, start, end))
