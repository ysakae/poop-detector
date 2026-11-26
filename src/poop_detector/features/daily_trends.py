"""日ごとの移動・睡眠・排泄の回数を集め、その犬のいつもの値と比べて外れた日を見つける。

いつもの値は、その日より前の USUAL_DAYS 日のうち値のある日の中央値と、散らばり
（中央絶対偏差を標準偏差の目安に直した値。小さすぎるときは指標ごとの下限か、中央値の
SPREAD_RATIO 倍）から決める。値のある日が MIN_DAYS 日に満たないうちは判断しない。
獣医師の診断の代わりではなく、記録を見返すきっかけを示すだけ。
"""

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import median
from typing import Literal, Protocol, TypeAlias

from ..records.database import Database
from ..records.events import Store, utcnow
from .sleep_store import SleepStore

USUAL_DAYS = 14
MIN_DAYS = 7
# いつもの値から散らばりのこの倍数より離れたら、いつもと違うとみなす。
THRESHOLD = 2.5
# 散らばりの下限（中央値に対する割合）。毎日ほとんど同じ値でも、少しの違いで知らせない。
SPREAD_RATIO = 0.25
# 中央絶対偏差を、正規分布の標準偏差の目安に直す係数。
MAD_SCALE = 1.4826
# 1日のうち、この割合を観測できた日だけを数える（睡眠と移動）。
SLEEP_COVERAGE = 0.8
MOVES_COVERAGE = 0.5
DAY_SECONDS = 86400
DAY_MINUTES = 1440

Direction: TypeAlias = Literal["high", "low"]
# 日ごとの指標の値。観測が足りない日は None。
DayValues: TypeAlias = dict[str, float | None]


@dataclass(frozen=True)
class Metric:
    """指標の名前・単位と、散らばりの下限（指標の単位）。"""

    label: str
    unit: str
    min_spread: float


METRICS = {
    # 床面のマス目を通過した回数。観測できた時間から1日分に直す。
    "moves": Metric("移動", "回", 20),
    "sleep": Metric("睡眠", "分", 60),
    "feces": Metric("うんち", "回", 1),
    "urine": Metric("おしっこ", "回", 1),
}
DIRECTIONS = {"high": "多い", "low": "少ない"}


@dataclass(frozen=True)
class Deviation:
    """ある日の指標が、いつもの値から外れたこと。"""

    metric: str
    day: date
    value: float
    usual: float
    direction: Direction

    @property
    def text(self) -> str:
        return f"{METRICS[self.metric].label}が{DIRECTIONS[self.direction]}"


def deviation(values: Mapping[date, float | None], day: date, metric: str) -> Deviation | None:
    """day の値が、その前の USUAL_DAYS 日のいつもの値から外れていれば返す。"""
    value = values.get(day)
    if value is None:
        return None
    before = [values.get(day - timedelta(days=offset)) for offset in range(1, USUAL_DAYS + 1)]
    usual_values = [v for v in before if v is not None]
    if len(usual_values) < MIN_DAYS:
        return None
    usual = median(usual_values)
    mad = median(abs(v - usual) for v in usual_values) * MAD_SCALE
    spread = max(mad, METRICS[metric].min_spread, abs(usual) * SPREAD_RATIO)
    score = (value - usual) / spread
    if abs(score) < THRESHOLD:
        return None
    return Deviation(metric, day, value, usual, "high" if score > 0 else "low")


def unusual(days: Mapping[date, DayValues], day: date) -> list[Deviation]:
    """day のすべての指標のうち、いつもと違うもの（METRICS の順）。"""
    found = []
    for metric in METRICS:
        values = {d: v.get(metric) for d, v in days.items()}
        result = deviation(values, day, metric)
        if result is not None:
            found.append(result)
    return found


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min).astimezone()


def gather(root: str | Path, store: Store, first: date, last: date) -> dict[date, DayValues]:
    """first から last の日ごとの指標を集める。行動マップのデータベースがなければ作らない。"""
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    result: dict[date, DayValues] = {d: dict.fromkeys(METRICS) for d in days}
    for day in days:
        result[day]["feces"] = 0
        result[day]["urine"] = 0
    for row in store.records_between(first, last):
        stamp = datetime.fromisoformat(row["actual_start"] or row["timestamp"]).astimezone()
        kind = row["excretion_type"]
        values = result.get(stamp.date())
        if values is None:
            continue
        for metric in ("feces", "urine"):
            if kind in (metric, "both"):
                values[metric] = (values[metric] or 0) + 1
    _sleep(SleepStore(store.database), first, last, result)
    _moves(Path(root) / "activity.sqlite3", first, last, result)
    return result


def _sleep(sleep: SleepStore, first: date, last: date, result: dict[date, DayValues]) -> None:
    asleep: dict[date, int] = {}
    observed: dict[date, int] = {}
    for record in sleep.minutes(_midnight(first), _midnight(last + timedelta(days=1))):
        if record.state == "unknown":
            continue
        day = record.minute.astimezone().date()
        observed[day] = observed.get(day, 0) + 1
        if record.state == "asleep":
            asleep[day] = asleep.get(day, 0) + 1
    for day, minutes in observed.items():
        if day in result and minutes >= SLEEP_COVERAGE * DAY_MINUTES:
            result[day]["sleep"] = asleep.get(day, 0)


def _moves(path: Path, first: date, last: date, result: dict[date, DayValues]) -> None:
    if not path.is_file():
        return
    params = (first.isoformat(), last.isoformat())
    with closing(sqlite3.connect(path, timeout=0.25)) as db:
        coverage = dict(
            db.execute(
                "SELECT day, SUM(sampled) FROM activity_coverage "
                "WHERE day BETWEEN ? AND ? GROUP BY day",
                params,
            ).fetchall()
        )
        entries = dict(
            db.execute(
                "SELECT day, SUM(entries) FROM activity_cells "
                "WHERE day BETWEEN ? AND ? GROUP BY day",
                params,
            ).fetchall()
        )
    for text, sampled in coverage.items():
        day = date.fromisoformat(text)
        if day in result and sampled >= MOVES_COVERAGE * DAY_SECONDS:
            result[day]["moves"] = entries.get(text, 0) * DAY_SECONDS / sampled


class TrendNotifier(Protocol):
    """いつもと違う日が続いたことを知らせる通知先。"""

    def send_trend(self, labels: list[str], day: date) -> bool: ...


class TrendNotices:
    """知らせた（日, 指標）を trend_notices に残し、同じことを繰り返し知らせない。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def notify(
        self, days: Mapping[date, DayValues], today: date, notifier: TrendNotifier
    ) -> list[str]:
        """昨日とおととい、同じ向きに外れた指標を一度だけ知らせ、知らせた指標を返す。"""
        yesterday = today - timedelta(days=1)
        before = {(d.metric, d.direction) for d in unusual(days, yesterday - timedelta(days=1))}
        found = [d for d in unusual(days, yesterday) if (d.metric, d.direction) in before]
        with self.database.connect() as db:
            done = {
                row["metric"]
                for row in db.execute(
                    "SELECT metric FROM trend_notices WHERE day = ?", (yesterday.isoformat(),)
                )
            }
        fresh = [d for d in found if d.metric not in done]
        if not fresh or not notifier.send_trend([d.text for d in fresh], today):
            return []
        with self.database.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO trend_notices (day, metric, direction, notified_at) "
                "VALUES (?, ?, ?, ?)",
                [(yesterday.isoformat(), d.metric, d.direction, utcnow()) for d in fresh],
            )
        return [d.metric for d in fresh]
