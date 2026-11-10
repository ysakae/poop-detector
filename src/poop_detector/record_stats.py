"""確定した排泄記録をローカル時刻の日付と時刻で集計する。"""

from collections import Counter

from .timezones import to_local


def summarize_records(records, first_day, last_day):
    daily = Counter()
    hourly = [Counter() for _ in range(24)]
    recorded_days = set()
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
