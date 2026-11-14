"""確定した排泄記録の書き出し（CSV）と、期間の集計（動物病院に見せる印刷用ページの値）。

期間と時刻はローカル時刻（timezones.to_local）で扱う。記録の開始時刻（なければ記録した時刻）の
ローカルの日付で期間に入るかを決める。CSV は表計算ソフトでそのまま開けるよう、BOM 付きの
UTF-8・CRLF で書き、数式として動きうるセルの先頭には ' を付ける。
"""

import csv
import io
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..errors import InvalidInputError
from ..timezones import to_local
from .events import EXCRETION_LOCATIONS, EXCRETION_TYPES
from .weights import Weight

CSV_HEADER: tuple[str, ...] = (
    "日付",
    "開始",
    "完了",
    "所要（秒）",
    "種類",
    "場所",
    "メモ",
    "記録の由来",
    "ID",
)
KIND_LABELS = {"SUSPECTED": "検知", "MANUAL": "手入力", "MISSED": "見逃し", "VIDEO": "動画"}
NO_LOCATION = "記録なし"
DEFAULT_DAYS = 30
MAX_DAYS = 3660
# 表計算ソフトが数式として扱う先頭の文字（CSV インジェクション対策）。
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class HourCount:
    """1時間ごとの件数。排便・排尿の両方の記録は両方に数える。"""

    hour: int
    feces: int
    urine: int
    unspecified: int


@dataclass(frozen=True)
class DayCount:
    """1日ごとの記録の件数と種類別の件数。"""

    day: date
    total: int
    feces: int
    urine: int
    unspecified: int


@dataclass(frozen=True)
class PeriodSummary:
    """期間の集計。feces・urine は「排便・排尿」の記録を両方に数えた件数。"""

    first_day: date
    last_day: date
    total: int
    feces: int
    urine: int
    unspecified: int
    recorded_days: int
    locations: dict[str, int]
    hourly: tuple[HourCount, ...]
    daily: tuple[DayCount, ...]

    @property
    def days(self) -> int:
        return (self.last_day - self.first_day).days + 1

    @property
    def per_day(self) -> float:
        return self.total / self.days


def parse_period(start: str, end: str, *, today: date) -> tuple[date, date]:
    """入力欄の YYYY-MM-DD の期間。空なら today までの30日間。"""
    try:
        last = date.fromisoformat(end) if end else today
        first = date.fromisoformat(start) if start else last - timedelta(days=DEFAULT_DAYS - 1)
    except ValueError:
        raise InvalidInputError("期間は YYYY-MM-DD の形で指定してください") from None
    if first > last:
        raise InvalidInputError("期間の終わりは始まり以降にしてください")
    if (last - first).days + 1 > MAX_DAYS:
        raise InvalidInputError(f"期間は{MAX_DAYS}日以内にしてください")
    return first, last


def _started(row: Mapping[str, Any]) -> datetime:
    return to_local(row["actual_start"] or row["timestamp"])


def in_period(
    records: Iterable[Mapping[str, Any]], first_day: date, last_day: date
) -> list[Mapping[str, Any]]:
    """期間に入る記録を、開始の順に返す。"""
    return sorted(
        (row for row in records if first_day <= _started(row).date() <= last_day), key=_started
    )


def _cell(value: str) -> str:
    return "'" + value if value.startswith(FORMULA_PREFIXES) else value


def weights_csv(rows: Iterable[Weight]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("測定日", "体重（g）", "メモ"))
    for row in rows:
        writer.writerow((row.day.isoformat(), row.grams, _cell(row.note)))
    return output.getvalue().encode("utf-8-sig")


def _row(row: Mapping[str, Any]) -> list[str]:
    start = _started(row)
    end = to_local(row["actual_end"]) if row["actual_end"] else None
    seconds = "" if end is None else str(round((end - start).total_seconds()))
    return [
        start.date().isoformat(),
        start.isoformat(),
        "" if end is None else end.isoformat(),
        seconds,
        EXCRETION_TYPES.get(row["excretion_type"], row["excretion_type"]),
        EXCRETION_LOCATIONS.get(row["location"] or "", ""),
        _cell(row["note"] or ""),
        KIND_LABELS.get(row["kind"], row["kind"]),
        row["id"],
    ]


def records_csv(records: Iterable[Mapping[str, Any]], first_day: date, last_day: date) -> bytes:
    """期間の確定記録の CSV（BOM 付き UTF-8、CRLF）。"""
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    writer.writerows(_row(row) for row in in_period(records, first_day, last_day))
    return ("﻿" + text.getvalue()).encode("utf-8")


def _kinds(row: Mapping[str, Any]) -> tuple[str, ...]:
    kind = row["excretion_type"]
    return ("feces", "urine") if kind == "both" else (kind,)


def summarize_period(
    records: Iterable[Mapping[str, Any]], first_day: date, last_day: date
) -> PeriodSummary:
    selected = in_period(records, first_day, last_day)
    kinds: Counter[str] = Counter()
    hourly = [Counter[str]() for _ in range(24)]
    daily: dict[date, Counter[str]] = {}
    locations: Counter[str] = Counter()
    for row in selected:
        started = _started(row)
        found = _kinds(row)
        kinds.update(found)
        hourly[started.hour].update(found)
        day = daily.setdefault(started.date(), Counter())
        day.update(found)
        day["total"] += 1
        locations[EXCRETION_LOCATIONS.get(row["location"] or "", NO_LOCATION)] += 1
    days = [first_day + timedelta(days=offset) for offset in range((last_day - first_day).days + 1)]
    return PeriodSummary(
        first_day=first_day,
        last_day=last_day,
        total=len(selected),
        feces=kinds["feces"],
        urine=kinds["urine"],
        unspecified=kinds["unspecified"],
        recorded_days=len(daily),
        locations=dict(locations),
        hourly=tuple(
            HourCount(hour, count["feces"], count["urine"], count["unspecified"])
            for hour, count in enumerate(hourly)
        ),
        daily=tuple(
            DayCount(
                day,
                daily.get(day, Counter())["total"],
                daily.get(day, Counter())["feces"],
                daily.get(day, Counter())["urine"],
                daily.get(day, Counter())["unspecified"],
            )
            for day in days
        ),
    )
