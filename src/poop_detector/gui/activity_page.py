"""登録した床面に滞在時間と通過回数を重ねる。"""

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

from ..features.activity_map import GRID, ActivityLayout, ActivityStore
from ..timezones import local_today, timezone_label
from .templating import render_template

PERIODS = ("1", "7", "30")


def heatmap(layout: ActivityLayout, cells: Sequence[Sequence[Any]], mode: str) -> dict[str, Any]:
    """マス目ごとの滞在時間（mode=stay）か通過回数（mode=pass）の、描く色と濃さ。"""
    metric = 3 if mode == "pass" else 2
    total = sum(row[metric] for row in cells)
    outline = layout.map_outline()
    drawn = []
    for row in cells:
        amount = row[metric]
        if amount <= 0:
            continue
        percent = 100 * amount / total
        drawn.append(
            {
                "x": row[0],
                "y": row[1],
                "amount": amount,
                "percent": percent,
                "opacity": min(0.9, 0.2 + 0.7 * percent / 10),
            }
        )
    return {
        # 色はテーマのトークン（style.css の --heat-pass・--heat-stay）で決める。
        "css": "heat-pass" if mode == "pass" else "heat-stay",
        "unit": "回" if mode == "pass" else "秒",
        "outline": " ".join(f"{round(x * GRID, 5):g},{round(y * GRID, 5):g}" for x, y in outline)
        if outline is not None
        else "",
        "cells": drawn,
    }


def render_activity(
    root: str | Path, query: Mapping[str, list[str]], *, title: str | None = None
) -> str:
    from .pages import APP_TITLE

    page = partial(render_template, "activity.html", title=title or APP_TITLE)
    try:
        layout = ActivityLayout.load(root)
    except (OSError, ValueError, TypeError, KeyError):
        return page(registered=False)
    try:
        last_day = date.fromisoformat(query.get("date", [""])[0])
    except ValueError:
        last_day = local_today()
    days = query.get("days", ["7"])[0]
    if days not in PERIODS:
        days = "7"
    first_day = date.fromordinal(max(1, last_day.toordinal() - int(days) + 1))
    mode = "pass" if query.get("mode") == ["pass"] else "stay"
    form = {
        "registered": True,
        "mode": mode,
        "last_day": last_day,
        "first_day": first_day,
        "days": days,
        "periods": PERIODS,
        "timezone": timezone_label(),
    }
    try:
        cells, sampled, located = ActivityStore(root).summary(layout.id, first_day, last_day)
    except sqlite3.Error:
        return page(failed=True, **form)
    return page(
        failed=False,
        sampled=sampled,
        located=located,
        passes=sum(row[3] for row in cells),
        grid=GRID,
        layout_id=layout.id,
        heat=heatmap(layout, cells, mode),
        **form,
    )
