"""睡眠の画面に出す値（時間帯ごとのヒートマップ、日ごとの合計、普段との違い）を集計する。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import mean
from typing import TYPE_CHECKING, Any

from ..config import Config, ConfigError, parse_daytime
from ..features.sleep_store import SleepStore
from ..timezones import local_today
from .templating import render_template

if TYPE_CHECKING:
    from .server import GuiApp

PERIODS = (7, 28, 91)
DEFAULT_PERIOD = 28
LABEL_EVERY = 7  # 日付の見出しを何日おきに付けるか
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


def duration_text(minutes: float) -> str:
    """分を「12時間30分」「45分」の形にする。"""
    total = round(minutes)
    hours, rest = divmod(total, 60)
    return f"{hours}時間{rest}分" if hours else f"{rest}分"


def signed_duration_text(minutes: float) -> str:
    """符号付きの「+40分」「-1時間5分」。"""
    sign = "-" if round(minutes) < 0 else "+"
    return f"{sign}{duration_text(abs(minutes))}"


def _percent_text(ratio: float) -> str:
    value = round(ratio * 100)
    return f"{'-' if value < 0 else '+'}{abs(value)}%"


def trend_text(trend: Trend) -> str:
    """直近7日の平均と、いつもとの違いを一文にする。"""
    if trend.recent_minutes is None:
        return "直近7日は、記録が足りる日がないため平均を出せません。"
    recent = f"直近{RECENT_DAYS}日の平均 {duration_text(trend.recent_minutes)}"
    if trend.diff_minutes is None or trend.diff_ratio is None:
        return f"{recent}（比べる過去の記録が足りません）"
    return (
        f"{recent}（いつもより {signed_duration_text(trend.diff_minutes)}、"
        f"{_percent_text(trend.diff_ratio)}）"
    )


def _cell_title(cell: HourCell) -> str:
    head = f"{cell.day.month}/{cell.day.day} {cell.hour}時"
    if cell.level is None:
        return f"{head} 記録なし／記録 {cell.observed_minutes}分"
    text = f"{head} 睡眠 {cell.asleep_minutes}分／記録 {cell.observed_minutes}分"
    if cell.detection_ratio is not None:
        text += f"／検出 {round(cell.detection_ratio * 100)}%"
    return text


def _header_cells(days: list[date]) -> list[dict[str, Any]]:
    """日付の見出し。LABEL_EVERY 日ごとの先頭の列に日付を置き、その週の列数だけ横に結合する。"""
    return [
        {
            "text": f"{days[start].month}/{days[start].day}",
            "span": min(LABEL_EVERY, len(days) - start),
        }
        for start in range(0, len(days), LABEL_EVERY)
    ]


def _grid(view: SleepView) -> dict[str, Any]:
    return {
        "labels": _header_cells(view.days),
        "rows": [
            {
                "hour": hour,
                "cells": [
                    {
                        "css": "sleep-none" if cell.level is None else f"sleep-l{cell.level}",
                        "title": _cell_title(cell),
                    }
                    for cell in row
                ],
            }
            for hour, row in enumerate(view.rows)
        ],
    }


def _summary_rows(view: SleepView) -> list[dict[str, Any]]:
    return [
        {
            "day": f"{s.day.month}/{s.day.day}",
            "asleep": duration_text(s.asleep_minutes),
            "daytime": duration_text(s.daytime_minutes),
            "observed": duration_text(s.observed_minutes),
            "reference": not s.reliable,
        }
        for s in reversed(view.summaries)
    ]


def _daytime(app: "GuiApp") -> tuple[time, time]:
    try:
        config = Config.load(app.config_path) if app.config_path is not None else Config()
    except (OSError, ConfigError):
        config = Config()
    return parse_daytime(config.sleep_daytime)


def render_sleep(app: "GuiApp", query: Mapping[str, list[str]]) -> str:
    """睡眠の草の画面。期間は ?days=（7・28・91。ほかは28）。"""
    requested = query.get("days", [""])[0]
    period = int(requested) if requested in {str(p) for p in PERIODS} else DEFAULT_PERIOD
    daytime = _daytime(app)
    view = sleep_view(app.sleep, local_today(), period, daytime)
    return render_template(
        "sleep.html",
        title=app.title,
        period=period,
        periods=PERIODS,
        empty=not any(s.observed_minutes for s in view.summaries),
        trend=trend_text(view.trend),
        daytime_range=f"{daytime[0]:%H:%M}〜{daytime[1]:%H:%M}",
        grid=_grid(view),
        summaries=_summary_rows(view),
    )
