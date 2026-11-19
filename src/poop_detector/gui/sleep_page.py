"""睡眠の画面に出す値（時間帯ごとのヒートマップ、日ごとの合計、普段との違い）を集計する。"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import mean

from ..features.sleep_store import SleepStore

PERIODS = (7, 28, 91)
RECENT_DAYS = 7
USUAL_DAYS = 28
MINUTES_PER_DAY = 1440
RELIABLE_RATIO = 0.8
UNKNOWN_LIMIT = 30  # 時間のうち記録できなかった分がこれ以上なら「記録なし」
LEVEL_LIMITS = (0, 15, 30, 45)  # 以下の分数ごとに濃さ 0〜3、超えれば 4


@dataclass(frozen=True)
class HourCell:
    """ある日のある時（ローカル時刻）の1マス。level は 0〜4、記録なしは None。"""

    day: date
    hour: int
    asleep_minutes: int
    observed_minutes: int
    detection_ratio: float | None
    level: int | None


@dataclass(frozen=True)
class DaySummary:
    """1日の睡眠の合計。reliable は記録できた分が1日の80%以上のとき。"""

    day: date
    asleep_minutes: int
    observed_minutes: int
    daytime_minutes: int
    reliable: bool


@dataclass(frozen=True)
class Trend:
    """直近7日と、その前の28日の平均（どちらも記録の足りる日だけ）の違い。"""

    recent_minutes: float | None
    usual_minutes: float | None
    diff_minutes: float | None
    diff_ratio: float | None


@dataclass(frozen=True)
class SleepView:
    """睡眠の画面の値。rows[hour][i] は days[i] の hour 時のマス。"""

    days: list[date]
    rows: list[list[HourCell]]
    summaries: list[DaySummary]
    trend: Trend
    period: int


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time()).astimezone()


def _level(asleep: int, observed: int) -> int | None:
    if 60 - observed >= UNKNOWN_LIMIT:
        return None
    for level, limit in enumerate(LEVEL_LIMITS):
        if asleep <= limit:
            return level
    return len(LEVEL_LIMITS)


def _mean_of_reliable(summaries: dict[date, DaySummary], days: list[date]) -> float | None:
    values = [summaries[d].asleep_minutes for d in days if d in summaries and summaries[d].reliable]
    return mean(values) if values else None


def _trend(summaries: dict[date, DaySummary], last_day: date) -> Trend:
    recent_days = [last_day - timedelta(days=i) for i in range(RECENT_DAYS)]
    usual_days = [last_day - timedelta(days=RECENT_DAYS + i) for i in range(USUAL_DAYS)]
    recent = _mean_of_reliable(summaries, recent_days)
    usual = _mean_of_reliable(summaries, usual_days)
    if recent is None or usual is None:
        return Trend(recent, usual, None, None)
    diff = recent - usual
    return Trend(recent, usual, diff, diff / usual if usual else None)


def sleep_view(
    store: SleepStore, last_day: date, period: int, daytime: tuple[time, time]
) -> SleepView:
    """last_day までの period 日分の画面の値を作る。普段との比較のため35日分は必ず読む。"""
    if period not in PERIODS:
        raise ValueError(f"期間は {PERIODS} のどれかにしてください: {period}")
    days = [last_day - timedelta(days=period - 1 - i) for i in range(period)]
    read_from = last_day - timedelta(days=max(period, RECENT_DAYS + USUAL_DAYS) - 1)
    end = _local_midnight(last_day + timedelta(days=1))
    asleep: dict[tuple[date, int], int] = {}
    observed: dict[tuple[date, int], int] = {}
    daytime_asleep: dict[date, int] = {}
    for record in store.minutes(_local_midnight(read_from), end):
        if record.state == "unknown":
            continue
        local = record.minute.astimezone()
        key = (local.date(), local.hour)
        observed[key] = observed.get(key, 0) + 1
        if record.state == "asleep":
            asleep[key] = asleep.get(key, 0) + 1
            if daytime[0] <= local.time() < daytime[1]:
                daytime_asleep[key[0]] = daytime_asleep.get(key[0], 0) + 1
    ratios: dict[tuple[date, int], float] = {}
    shown_from = _local_midnight(days[0])
    for hour, (present, detected) in store.detection_hours(shown_from, end).items():
        local = hour.astimezone()
        if present > 0:
            ratios[(local.date(), local.hour)] = detected / present
    summaries = {
        day: DaySummary(
            day=day,
            asleep_minutes=sum(asleep.get((day, h), 0) for h in range(24)),
            observed_minutes=sum(observed.get((day, h), 0) for h in range(24)),
            daytime_minutes=daytime_asleep.get(day, 0),
            reliable=sum(observed.get((day, h), 0) for h in range(24))
            >= RELIABLE_RATIO * MINUTES_PER_DAY,
        )
        for day in [read_from + timedelta(days=i) for i in range((last_day - read_from).days + 1)]
    }
    rows = [
        [
            HourCell(
                day=day,
                hour=hour,
                asleep_minutes=asleep.get((day, hour), 0),
                observed_minutes=observed.get((day, hour), 0),
                detection_ratio=ratios.get((day, hour)),
                level=_level(asleep.get((day, hour), 0), observed.get((day, hour), 0)),
            )
            for day in days
        ]
        for hour in range(24)
    ]
    return SleepView(
        days=days,
        rows=rows,
        summaries=[summaries[d] for d in days],
        trend=_trend(summaries, last_day),
        period=period,
    )
