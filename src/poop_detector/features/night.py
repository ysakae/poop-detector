"""夜ごとの「夜間のまとめ」の時間帯・睡眠の数字・朝の知らせ（night.enabled）。

夜の区間は時刻から決める（night.hours。既定 22:00-07:00 で、日をまたいでよい）。
夜の名前は始まった日で、静止画は digest/night-YYYYMMDD/ に残す（features.digest）。
睡眠の数字は睡眠の記録（features.sleep_store）から求め、目安として示す。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Protocol

from ..records.database import Database
from ..records.events import Store, utcnow
from .daily_trends import USUAL_DAYS, Deviation, deviation
from .sleep_store import MinuteRecord, SleepStore

# 起きていた時間がこの分数以上続いたら「起きた」と数える（身じろぎを数えない）。
AWAKE_MINUTES = 5
# 夜の時間帯のうちこの割合を観測できた夜だけ、いつもとの違いを比べる。
NIGHT_COVERAGE = 0.8
_HOURS = re.compile(r"(\d{2}:\d{2})-(\d{2}:\d{2})")


def parse_hours(value: str) -> tuple[time, time]:
    """「HH:MM-HH:MM」の夜の時間帯を（始め, 終わり）にする。日をまたいでよい。"""
    match = _HOURS.fullmatch(value)
    if match is None:
        raise ValueError(f"夜の時間帯は HH:MM-HH:MM の形で書いてください: {value!r}")
    try:
        start, end = (time.fromisoformat(part) for part in match.groups())
    except ValueError:
        raise ValueError(f"夜の時間帯に存在しない時刻があります: {value!r}") from None
    if start == end:
        raise ValueError("夜の時間帯の始めと終わりを同じにはできません")
    return start, end


def night_window(night: date, hours: str) -> tuple[datetime, datetime]:
    """night の日に始まる夜の（始め, 終わり）。終わりが始めより前の時刻なら翌日。"""
    start, end = parse_hours(hours)
    begin = datetime.combine(night, start).astimezone()
    finish_day = night + timedelta(days=1) if end <= start else night
    return begin, datetime.combine(finish_day, end).astimezone()


def night_of(now: datetime, hours: str) -> date | None:
    """now が夜の時間帯の中なら、その夜の名前（始まった日）。外なら None。"""
    local = now.astimezone()
    for night in (local.date(), local.date() - timedelta(days=1)):
        begin, finish = night_window(night, hours)
        if begin <= local < finish:
            return night
    return None


def latest_night(now: datetime, hours: str) -> date:
    """now より前に終わった、いちばん新しい夜。"""
    local = now.astimezone()
    for offset in range(3):
        night = local.date() - timedelta(days=offset)
        if night_window(night, hours)[1] <= local:
            return night
    return local.date() - timedelta(days=3)


def night_id(night: date) -> str:
    """静止画のフォルダーの名前（features.digest.DigestSnapshots）。"""
    return f"night-{night:%Y%m%d}"


@dataclass(frozen=True)
class SleepSummary:
    """夜の睡眠の数字。awake_spans は、眠ってから起きていた AWAKE_MINUTES 以上の時間帯。"""

    asleep_minutes: int
    observed_minutes: int
    awakenings: int
    longest_minutes: int
    awake_spans: list[tuple[datetime, datetime]] = field(default_factory=list)


def sleep_summary(records: Sequence[MinuteRecord]) -> SleepSummary:
    """分ごとの睡眠の記録から、夜の数字を求める（副作用のない関数）。"""
    observed = [r for r in records if r.state != "unknown"]
    asleep = [r for r in observed if r.state == "asleep"]
    if not asleep:
        return SleepSummary(0, len(observed), 0, 0)
    first, last = asleep[0].minute, asleep[-1].minute
    # 眠ってから最後に寝ていた分までの間の、起きていた分のつながり。
    runs: list[tuple[datetime, datetime]] = []
    for record in observed:
        if record.state == "asleep" or not first < record.minute < last:
            continue
        end = record.minute + timedelta(minutes=1)
        if runs and runs[-1][1] == record.minute:
            runs[-1] = (runs[-1][0], end)
        else:
            runs.append((record.minute, end))
    spans = [run for run in runs if run[1] - run[0] >= timedelta(minutes=AWAKE_MINUTES)]
    # いちばん長く続けて寝た時間は、起きたと数えない短い身じろぎをまたいで測る。
    edges = [first, *[t for span in spans for t in span], last + timedelta(minutes=1)]
    longest = max(
        (edges[i + 1] - edges[i] for i in range(0, len(edges) - 1, 2)), default=timedelta()
    )
    return SleepSummary(
        len(asleep), len(observed), len(spans), int(longest.total_seconds() // 60), spans
    )


def night_records(store: Store, night: date, hours: str) -> list[MinuteRecord]:
    begin, finish = night_window(night, hours)
    return SleepStore(store.database).minutes(begin, finish)


def sleep_deviation(store: Store, night: date, hours: str) -> Deviation | None:
    """その夜の寝ていた時間が、前の USUAL_DAYS 夜のいつもの値から外れていれば返す。"""
    values: dict[date, float | None] = {}
    for offset in range(USUAL_DAYS + 1):
        day = night - timedelta(days=offset)
        begin, finish = night_window(day, hours)
        summary = sleep_summary(night_records(store, day, hours))
        window = (finish - begin).total_seconds() / 60
        enough = summary.observed_minutes >= NIGHT_COVERAGE * window
        values[day] = summary.asleep_minutes if enough else None
    return deviation(values, night, "sleep")


class NightNotifier(Protocol):
    """朝に昨夜のまとめを知らせる通知先。"""

    def send_night_summary(self, night: date, asleep_minutes: int, awakenings: int) -> bool: ...


class NightNotices:
    """朝の知らせを送った夜を night_notices に残し、同じ夜を繰り返し知らせない。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def notified(self, night: date) -> bool:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 FROM night_notices WHERE night=?", (night.isoformat(),)
            ).fetchone()
        return row is not None

    def notify(
        self, store: Store, hours: str, now: datetime, notifier: NightNotifier
    ) -> date | None:
        """終わったいちばん新しい夜をまだ知らせていなければ知らせ、知らせた夜を返す。"""
        night = latest_night(now, hours)
        if now - night_window(night, hours)[1] > timedelta(hours=12) or self.notified(night):
            return None
        summary = sleep_summary(night_records(store, night, hours))
        if summary.observed_minutes == 0:
            return None
        if not notifier.send_night_summary(night, summary.asleep_minutes, summary.awakenings):
            return None
        with self.database.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO night_notices (night, notified_at) VALUES (?, ?)",
                (night.isoformat(), utcnow()),
            )
        return night


def length_text(minutes: float) -> str:
    """「6時間30分」のような長さの表示。"""
    hours, rest = divmod(round(minutes), 60)
    return f"{hours}時間{rest:02d}分"


def snapshot_targets(
    night_hours: str | None, now: datetime, away_id: str | None
) -> Mapping[str, None]:
    """いま静止画を残す先（夜・外出の区間のフォルダーの名前）。順序つきで重なりを除く。"""
    targets: dict[str, None] = {}
    if night_hours:
        night = night_of(now, night_hours)
        if night is not None:
            targets[night_id(night)] = None
    if away_id is not None:
        targets[away_id] = None
    return targets
