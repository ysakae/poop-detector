"""体重の測定値を、日数とグラムに比例するグラフの座標に変換する。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import ClassVar

from ..records.weights import Weight


@dataclass(frozen=True)
class WeightPoint:
    """測定点の座標、表示値、詳細の表示位置。"""

    x: int
    y: int
    day: date
    grams: int
    tooltip_x: int
    tooltip_y: int


@dataclass(frozen=True)
class WeightChart:
    """測定点と両軸の目盛りを持つ描画用の値。"""

    left: ClassVar[int] = 56
    right: ClassVar[int] = 384
    top: ClassVar[int] = 24
    bottom: ClassVar[int] = 184
    points: list[WeightPoint]
    dates: list[tuple[int, str]]
    grams: list[tuple[int, int]]


def _gram_step(span: int) -> int:
    # 4区間を目安とし、目盛りは 1、2、5 のいずれかの桁で揃える。
    target = (span + 3) // 4
    magnitude = int(10 ** (len(str(target)) - 1))
    return next(magnitude * factor for factor in (1, 2, 5, 10) if magnitude * factor >= target)


def weight_chart(rows: Sequence[Weight]) -> WeightChart:
    """測定日順の行から、各点と目盛りの位置を求める。"""
    if not rows:
        return WeightChart([], [], [])
    first, last = rows[0].day, rows[-1].day
    days = (last - first).days
    low, high = min(row.grams for row in rows), max(row.grams for row in rows)
    padding = max(10, (high - low + 9) // 10)
    low, high = max(0, low - padding), high + padding
    step = _gram_step(high - low)
    low = low // step * step
    high = (high + step - 1) // step * step

    def x_position(day: date) -> int:
        width = WeightChart.right - WeightChart.left
        fraction = (day - first).days / days if days else 0.5
        return WeightChart.left + round(width * fraction)

    def y_position(grams: int) -> int:
        height = WeightChart.bottom - WeightChart.top
        return WeightChart.bottom - round(height * (grams - low) / (high - low))

    intervals = max(1, min(days, 3))
    ticks = [first + timedelta(days=round(days * index / intervals)) for index in range(intervals)]
    if days:
        ticks.append(last)
    date_format = "{0.month}/{0.day}" if first.year == last.year else "{0.year}/{0.month}/{0.day}"

    points = []
    for row in rows:
        x, y = x_position(row.day), y_position(row.grams)
        tooltip_x = min(max(x - 56, 8), 280) - x
        tooltip_y = -54 if y >= 62 else 12
        points.append(WeightPoint(x, y, row.day, row.grams, tooltip_x, tooltip_y))

    return WeightChart(
        points,
        [(x_position(day), date_format.format(day)) for day in ticks],
        [(y_position(grams), grams) for grams in range(high, low - 1, -step)],
    )
