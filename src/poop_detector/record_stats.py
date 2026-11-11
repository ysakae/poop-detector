"""確定した排泄記録をローカル時刻の日付と時刻で集計する。"""

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from .timezones import to_local


def summarize_records(
    records: Iterable[Mapping[str, Any]], first_day: date, last_day: date
) -> tuple[Counter[str], list[Counter[str]], int]:
    """期間内の記録を数え、最終日の種類別件数、時間帯別件数、記録のあった日数を返す。"""
    daily: Counter[str] = Counter()
    hourly: list[Counter[str]] = [Counter() for _ in range(24)]
    recorded_days: set[date] = set()
    for row in records:
        stamp = to_local(row["actual_start"] or row["timestamp"])
        day = stamp.date()
        if not first_day <= day <= last_day:
            continue
        recorded_days.add(day)
        kind = row["excretion_type"]
        kinds = ("feces", "urine") if kind == "both" else (kind,)
        hourly[stamp.hour].update(kinds)
        if day == last_day:
            daily.update(kinds)
    return daily, hourly, len(recorded_days)
