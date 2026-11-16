"""一覧に出す短い時刻（今日・きのう・月日）と、日ごとの見出しで区切る並べ方。

日付の境目はローカル時刻（timezones.py）で決める。正確な時刻は <time> の datetime と
title に残し、詳細の画面は従来どおり pages.local_time の形で出す。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..timezones import local_today, to_local

WEEKDAYS = "月火水木金土日"
UNKNOWN_DAY = "日時不明"


@dataclass(frozen=True)
class ListTime:
    """一覧の1件の時刻。text は日付付き、clock は日の見出しの下で使う時刻だけ。"""

    text: str
    clock: str
    day: str
    iso: str
    full: str


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "今日"
    if day == today - timedelta(days=1):
        return "きのう"
    weekday = WEEKDAYS[day.weekday()]
    if day.year == today.year:
        return f"{day.month}月{day.day}日（{weekday}）"
    return f"{day.year}年{day.month}月{day.day}日（{weekday}）"


def _date_prefix(stamp: datetime, today: date) -> str:
    day = stamp.date()
    if day in (today, today - timedelta(days=1)):
        return _day_label(day, today)
    if day.year == today.year:
        return f"{day.month}/{day.day}"
    return stamp.strftime("%Y/%m/%d")


def list_time(value: str | None, today: date | None = None) -> ListTime | None:
    """保存した UTC の時刻を、一覧用の短い表示にする。空なら None。"""
    if not value:
        return None
    stamp = to_local(value).replace(microsecond=0)
    current = today or local_today()
    clock = stamp.strftime("%H:%M")
    return ListTime(
        text=f"{_date_prefix(stamp, current)} {clock}",
        clock=clock,
        day=_day_label(stamp.date(), current),
        iso=stamp.isoformat(),
        full=stamp.strftime("%Y/%m/%d %H:%M:%S"),
    )


def by_day(items: Iterable[Mapping[str, Any]], key: str = "when") -> list[dict[str, Any]]:
    """並び順を保ったまま、日が変わるところで区切る。時刻のない件は「日時不明」にまとめる。"""
    groups: list[dict[str, Any]] = []
    for item in items:
        when = item[key]
        day = when.day if when else UNKNOWN_DAY
        if not groups or groups[-1]["day"] != day:
            groups.append({"day": day, "rows": []})
        groups[-1]["rows"].append(item)
    return groups
