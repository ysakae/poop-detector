"""ラベル GUI の共通の文書の枠と、トップ（イベントのラベル付け）と排泄の記録の画面。"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any, TypeAlias
from urllib.parse import urlencode

from ..records.events import EXCRETION_LOCATIONS, EXCRETION_TYPES, EventRow, Store
from ..records.record_stats import summarize_records
from ..timezones import local_today, parse_local, timezone_label, to_local
from ..video.clip_storage import DEFAULT_DISK_MIB
from ..video.clips import ClipStore
from .clip_pages import capacity_info
from .templating import render_template

# parse_qs で読んだ問い合わせ文字列。同じ名前の値はリストになる。
Query: TypeAlias = Mapping[str, list[str]]
STATS_PERIODS = ("7", "30", "90")
APP_TITLE = "トイレ記録"


def local_time(value: str | None, form: bool = False) -> str:
    """保存した UTC の時刻を、画面表示用（form=True なら入力欄用）のローカル時刻にする。"""
    if not value:
        return ""
    stamp = to_local(value)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S" if form else "%Y/%m/%d %H:%M:%S")


def parse_time(value: str | None) -> str | None:
    """フォームの日時を、保存用のタイムゾーン付き ISO 8601 にする。"""
    if not value:
        return None
    return parse_local(value).isoformat()


def page_title(pet_name: str = "") -> str:
    return f"{pet_name}の{APP_TITLE}" if pet_name else APP_TITLE


def message_page(
    heading: str,
    message: str,
    back_url: str,
    back_text: str,
    *,
    title: str | None,
    wrap_link: bool = False,
) -> str:
    """保存や整理に失敗したときの画面。"""
    return render_template(
        "message.html",
        title=title or APP_TITLE,
        heading=heading,
        message=message,
        back_url=back_url,
        back_text=back_text,
        wrap_link=wrap_link,
    )


def save_failed_page(title: str | None) -> str:
    return message_page(
        "保存できませんでした",
        "ラベルと時刻を確認してください。完了は開始以降を指定します。",
        "/",
        "記録に戻る",
        title=title,
    )


FILTERS = {
    "unlabeled": "未ラベル",
    "all": "すべて",
    "true": "排泄あり",
    "feces": "排便",
    "urine": "排尿",
    "both": "排便・排尿",
    "manual": "手動記録",
    "false": "誤検知",
    "missed": "見逃し",
}
KIND_SUFFIXES = {"MISSED": "（見逃し）", "MANUAL": "（手動）", "VIDEO": "（映像）"}
SOURCES = {"MANUAL": "手動", "MISSED": "見逃し", "SUSPECTED": "検知を確認", "VIDEO": "映像で確認"}


def matches_filter(row: EventRow, mode: str) -> bool:
    """トップの「表示する記録」の絞り込み。"""
    if mode == "all":
        return True
    if mode == "missed":
        return row["kind"] == "MISSED"
    if mode == "manual":
        return row["kind"] in ("MANUAL", "VIDEO")
    if mode == "true":
        return row["feedback"] == 1
    if mode in ("feces", "urine", "both"):
        kind = row["excretion_type"]
        return row["feedback"] == 1 and (kind == mode or (mode != "both" and kind == "both"))
    if row["kind"] != "SUSPECTED":
        return False
    return (mode == "unlabeled" and row["feedback"] is None) or (
        mode == "false" and row["feedback"] == 0
    )


def label_text(row: EventRow) -> str:
    """一覧に出す記録の状態（排便、誤検知、未ラベルなど）。"""
    if row["feedback"] == 1:
        label = EXCRETION_TYPES[row["excretion_type"]]
    elif row["feedback"] == 0:
        label = "誤検知"
    else:
        label = "未ラベル"
    return label + KIND_SUFFIXES.get(row["kind"], "")


def selected_label(row: EventRow) -> str:
    if row["feedback"] == 1:
        return row["excretion_type"]
    return "false" if row["feedback"] == 0 else "unknown"


def event_detail(row: EventRow) -> dict[str, Any]:
    """選んだ記録の詳細と、ラベル入力欄の初期値。"""
    choices: list[tuple[str, str]] = [
        (value, "排便だった" if value == "feces" else text)
        for value, text in EXCRETION_TYPES.items()
    ]
    if row["kind"] == "SUSPECTED":
        choices += [("false", "誤検知"), ("unknown", "保留・未ラベル")]
    elif row["kind"] == "VIDEO":
        choices += [("unknown", "保留・未ラベル")]
    return {
        "id": row["id"],
        "time": local_time(row["timestamp"]),
        "image": row["image"],
        "editable": row["kind"] in ("SUSPECTED", "MANUAL", "MISSED", "VIDEO"),
        "choices": choices,
        "selected_label": selected_label(row),
        "location": row["location"],
        "times": [
            ("start", "排泄の開始", local_time(row["actual_start"], form=True)),
            ("end", "排泄の完了", local_time(row["actual_end"], form=True)),
        ],
        "note": row["note"],
    }


def render(
    store: Store,
    token: str,
    query: Query,
    disk_mb: float = DEFAULT_DISK_MIB,
    *,
    title: str | None = None,
) -> str:
    """トップ画面。イベントの一覧と、選んだイベントのラベル入力欄。"""
    mode = query.get("filter", ["unlabeled"])[0]
    if mode not in FILTERS:
        mode = "unlabeled"
    rows = [row for row in reversed(store.list()) if matches_filter(row, mode)]
    selected = query.get("id", [""])[0]
    row = next((r for r in rows if r["id"] == selected), rows[0] if rows else None)
    metrics = store.metrics()
    items = [
        {
            "active": row is not None and row["id"] == item["id"],
            "url": "/?" + urlencode({"filter": mode, "id": item["id"]}),
            "time": local_time(item["timestamp"]),
            "label": label_text(item),
        }
        for item in rows
    ]
    return render_template(
        "top.html",
        title=title or APP_TITLE,
        token=token,
        capacity=capacity_info(store.root, disk_mb),
        filters=list(FILTERS.items()),
        mode=mode,
        metrics=metrics,
        counts=metrics["excretion_counts"],
        saved=bool(query.get("saved")),
        items=items,
        row=event_detail(row) if row else None,
        locations=list(EXCRETION_LOCATIONS.items()),
        excretion_types=list(EXCRETION_TYPES.items()),
        timezone=timezone_label(),
    )


def record_stats(records: Iterable[EventRow], query: Query) -> dict[str, Any]:
    """記録の画面の、1日の件数と時間帯別の件数。"""
    try:
        day = date.fromisoformat(query.get("date", [""])[0])
    except ValueError:
        day = local_today()
    period = query.get("days", ["30"])[0]
    if period not in STATS_PERIODS:
        period = "30"
    start = date.fromordinal(max(1, day.toordinal() - int(period) + 1))
    daily, hourly, recorded_days = summarize_records(records, start, day)
    return {
        "day": day,
        "period": period,
        "periods": STATS_PERIODS,
        "start": start,
        "length": (day - start).days + 1,
        "daily": daily,
        "hourly": list(enumerate(hourly)),
        "recorded_days": recorded_days,
        "scale": max(1, *(counts[kind] for counts in hourly for kind in ("feces", "urine"))),
    }


def record_row(row: EventRow, videos: Sequence[str]) -> dict[str, Any]:
    recorded_at = local_time(row["actual_start"] or row["timestamp"])
    if row["kind"] == "VIDEO" and not row["actual_start"]:
        recorded_at += "（動画時刻・開始不明）"
    return {
        "cells": [
            recorded_at,
            local_time(row["actual_end"]),
            EXCRETION_TYPES[row["excretion_type"]],
            EXCRETION_LOCATIONS.get(row["location"] or "", "場所不明"),
            SOURCES[row["kind"]],
            row["note"],
        ],
        "videos": videos,
        "url": "/?" + urlencode({"filter": "all", "id": row["id"]}),
    }


def render_records(
    store: Store, clips: ClipStore, query: Query, *, title: str | None = None
) -> str:
    records = store.records()
    video_links = clips.labels.record_videos()
    return render_template(
        "records.html",
        title=title or APP_TITLE,
        stats=record_stats(records, query),
        timezone=timezone_label(),
        records=[record_row(row, video_links.get(row["id"], [])) for row in reversed(records)],
    )
