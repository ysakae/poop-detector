"""確定した排泄記録を日本時間の日付と時刻で集計する。"""

from collections import Counter
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


def summarize_records(records, first_day, last_day):
    daily = Counter()
    hourly = [Counter() for _ in range(24)]
    recorded_days = set()
    for row in records:
        stamp = datetime.fromisoformat(row["actual_start"] or row["timestamp"]).astimezone(JST)
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
