"""GUI のホーム（ダッシュボード）、検知の一覧（イベントのラベル付け）、排泄の記録の画面。"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any, TypeAlias
from urllib.parse import urlencode

from ..features.outages import OUTAGE_KINDS, Outage, format_duration
from ..records.care import CareEntry, feeding_text, format_grams, last_feeding, latest_entries
from ..records.care import entries as care_entries
from ..records.events import EXCRETION_LOCATIONS, EXCRETION_TYPES, EventRow, Store
from ..records.record_stats import summarize_records
from ..timezones import local_now, local_today, parse_local, timezone_label, to_local
from ..video.clip_storage import DEFAULT_DISK_MIB
from ..video.clips import ClipStore
from .clip_pages import capacity_info
from .list_time import ListTime, by_day, list_time
from .templating import render_template

# parse_qs で読んだ問い合わせ文字列。同じ名前の値はリストになる。
Query: TypeAlias = Mapping[str, list[str]]
STATS_PERIODS = ("7", "30", "90")
EVENTS_PAGE = "/records/events"
# 目視で確認した排泄を記録する画面。
NEW_RECORD_PAGE = "/records/new"
RECENT_RECORDS = 5
APP_TITLE = "トイレ記録"


def local_time(value: str | None, form: bool = False) -> str:
    """保存した UTC の時刻を、画面表示用（form=True なら入力欄用）のローカル時刻にする。"""
    if not value:
        return ""
    stamp = to_local(value)
    return stamp.strftime("%Y-%m-%dT%H:%M" if form else "%Y/%m/%d %H:%M:%S")


def outage_rows(outages: Iterable[Outage]) -> list[dict[str, str]]:
    """管理 › 稼働状況の「直近の停止」の行。続いている停止はいままでの長さを出す。"""
    return [
        {
            "kind": OUTAGE_KINDS.get(outage.kind, outage.kind),
            "started": local_time(outage.started_at),
            "ended": local_time(outage.ended_at) if outage.ended_at else "継続中",
            "duration": format_duration(outage.seconds()),
            "notified": "送信済み" if outage.notified_at else "未送信",
        }
        for outage in outages
    ]


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
        EVENTS_PAGE,
        "検知の一覧に戻る",
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
# 検知の画像の確認で、1回押して保存するボタン（値・文言・アイコン）。
EVENT_QUICK_LABELS: tuple[tuple[str, str, str], ...] = (
    ("feces", "排便", "feces"),
    ("urine", "排尿", "urine"),
    ("both", "両方", "paw"),
    ("false", "誤検知", "none"),
)
# 排泄を記録の、押すと保存するボタン。(type の値, 名前, アイコン)。
NEW_RECORD_BUTTONS: tuple[tuple[str, str, str], ...] = (
    *EVENT_QUICK_LABELS[:3],
    ("unspecified", "種類不明", "paw"),
)
# 「通知は来ましたか？」の答え。(action の値, 名前)。保存する値は従来の manual・missed。
NEW_RECORD_KINDS: tuple[tuple[str, str], ...] = (
    ("manual", "来た、または通知とは関係なく記録する"),
    ("missed", "来なかった（見逃しとして数える）"),
)
KIND_SUFFIXES = {"MISSED": "（見逃し）", "MANUAL": "（手動）", "VIDEO": "（映像）"}
SOURCES = {"MANUAL": "手動", "MISSED": "見逃し", "SUSPECTED": "検知を確認", "VIDEO": "映像で確認"}


def matches_filter(row: EventRow, mode: str) -> bool:
    """検知の一覧の「表示する記録」の絞り込み。"""
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


def event_item(row: EventRow, mode: str, active: bool) -> dict[str, Any]:
    """検知の一覧の1行。動画の一覧と同じく、サムネイル・時刻・状態のラベルで見せる。"""
    # 画像のない記録（手動・見逃し）は、種類のアイコンで見せる。
    icon = RECORD_ICONS.get(row["excretion_type"], "paw") if row["feedback"] == 1 else "camera"
    return {
        "active": active,
        "url": f"{EVENTS_PAGE}?" + urlencode({"filter": mode, "id": row["id"]}),
        "id": row["id"],
        "image": bool(row["image"]),
        "icon": icon,
        "when": list_time(row["timestamp"]),
        "label": label_text(row),
        "state": "warn" if row["feedback"] is None else "ok",
    }


def selected_label(row: EventRow) -> str:
    if row["feedback"] == 1:
        return row["excretion_type"]
    return "false" if row["feedback"] == 0 else "unknown"


def event_quick_buttons(row: EventRow, allowed: Sequence[str]) -> list[dict[str, str]]:
    """まだラベルのない検知に出す、1回押して保存するボタン（動画の quick_form と同じ形）。

    ボタンは /save に label の値を送るだけで、日時・メモ・場所は既存の値を保つ。
    この記録で選べない種類（動画からの記録の誤検知）のボタンは出さない。
    """
    if row["feedback"] is not None:
        return []
    return [
        {"value": value, "text": text, "icon": icon}
        for value, text, icon in EVENT_QUICK_LABELS
        if value in allowed
    ]


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
    editable = row["kind"] in ("SUSPECTED", "MANUAL", "MISSED", "VIDEO")
    return {
        "id": row["id"],
        "time": local_time(row["timestamp"]),
        "image": row["image"],
        "editable": editable,
        "quick": event_quick_buttons(row, [value for value, _ in choices]) if editable else [],
        "choices": choices,
        "selected_label": selected_label(row),
        "location": row["location"],
        "times": [
            ("start", "排泄の開始", local_time(row["actual_start"], form=True)),
            ("end", "排泄の完了", local_time(row["actual_end"], form=True)),
        ],
        "note": row["note"],
    }


def render_events(
    store: Store,
    token: str,
    query: Query,
    *,
    title: str | None = None,
    review: Mapping[str, Any] | None = None,
    waiting: int = 0,
) -> str:
    """検知の一覧。イベントの一覧と、選んだイベントのラベル入力欄。

    review は確認待ちから開いたときの帯（review_queue.review_bar）。waiting は確認待ちの件数。
    """
    mode = query.get("filter", ["unlabeled"])[0]
    if mode not in FILTERS:
        mode = "unlabeled"
    rows = [row for row in reversed(store.list()) if matches_filter(row, mode)]
    selected = query.get("id", [""])[0]
    row = next((r for r in rows if r["id"] == selected), rows[0] if rows else None)
    metrics = store.metrics()
    items = [event_item(item, mode, row is not None and row["id"] == item["id"]) for item in rows]
    return render_template(
        "events.html",
        title=title or APP_TITLE,
        token=token,
        filters=list(FILTERS.items()),
        mode=mode,
        metrics=metrics,
        counts=metrics["excretion_counts"],
        saved=bool(query.get("saved")),
        items=items,
        row=event_detail(row) if row else None,
        locations=list(EXCRETION_LOCATIONS.items()),
        timezone=timezone_label(),
        review=review,
        waiting=waiting,
    )


def render_new_record(token: str, *, title: str | None = None, manual_only: bool = False) -> str:
    """目視で確認した排泄（通常の手動記録と、通知されなかった見逃し）を記録する画面。

    見かけてすぐ記録できるよう、開始にはいまの時刻を入れておき、種類のボタンを押すと保存する。
    """
    return render_template(
        "record_new.html",
        manual_only=manual_only,
        title=title or APP_TITLE,
        token=token,
        locations=list(EXCRETION_LOCATIONS.items()),
        buttons=NEW_RECORD_BUTTONS,
        kinds=NEW_RECORD_KINDS,
        now=local_now().strftime("%Y-%m-%dT%H:%M"),
        timezone=timezone_label(),
    )


# ホームの「記録する」の入口。(URL, アイコン, 名前)。
HOME_QUICK: tuple[tuple[str, str, str], ...] = (
    ("/care#feeding", "food", "ごはん"),
    ("/care#sheet", "sheet", "シート交換"),
    ("/weight", "weight", "体重"),
    (NEW_RECORD_PAGE, "paw", "排泄"),
)
# 確定記録の種類ごとのアイコン（gui/icons.py の名前）。ない種類は肉球にする。
RECORD_ICONS = {"feces": "feces", "urine": "urine"}


def _recorded_notice(entry: CareEntry, day: str) -> dict[str, str]:
    """ホームから記録した直後の知らせ。何を記録したかと、直す画面の URL。"""
    detail = feeding_text(entry.food, entry.amount_dg) if entry.kind == "feeding" else ""
    name = "ごはん" if entry.kind == "feeding" else "シート交換"
    return {
        "text": f"{name}（{detail}）" if detail else name,
        "url": f"/care?day={day}&id={entry.id}",
    }


def _last_care(
    store: Store, records: Sequence[EventRow], today: date
) -> list[tuple[str, str, ListTime | None, str]]:
    """お世話の種類ごとに、ホームに出す最後の実施時刻をそろえる。"""
    entries = latest_entries(store.database)
    feeding = entries.get("feeding")
    sheet = entries.get("sheet")
    result = [
        (
            "ごはん",
            "food",
            list_time(feeding.at.isoformat(), today) if feeding else None,
            feeding_text("", feeding.amount_dg) if feeding else "",
        ),
    ]
    now = local_now()
    for label, kind in (("うんち", "feces"), ("おしっこ", "urine")):
        row = next(
            (
                row
                for row in reversed(records)
                if row["excretion_type"] in (kind, "both")
                and to_local(row["actual_start"] or row["timestamp"]) <= now
            ),
            None,
        )
        stamp = (row["actual_start"] or row["timestamp"]) if row else None
        result.append((label, kind, list_time(stamp, today), ""))
    result.append(
        ("シート交換", "sheet", list_time(sheet.at.isoformat(), today) if sheet else None, "")
    )
    return result


def render_home(
    store: Store,
    disk_mb: float = DEFAULT_DISK_MIB,
    *,
    title: str | None = None,
    pending: int = 0,
    manual_only: bool = False,
    token: str = "",
    recorded: str = "",
) -> str:
    """ホーム。確認待ちの件数、今日の回数と給餌量、記録の入口、直近の確定記録、ライブ映像。

    recorded は、ホームから記録した直後のお世話の記録の ID（知らせと「直す」の入口を出す）。
    """
    records = store.records()
    today = local_today()
    daily, _, _ = summarize_records(records, today, today)
    cared = care_entries(store.database, today)
    fed = [entry.amount_dg for entry in cared if entry.amount_dg]
    last = last_feeding(store.database)
    done = next((entry for entry in cared if recorded and entry.id == recorded), None)
    recent = [
        {
            "when": list_time(row["actual_start"] or row["timestamp"]),
            "type": EXCRETION_TYPES[row["excretion_type"]],
            "icon": RECORD_ICONS.get(row["excretion_type"], "paw"),
            "location": EXCRETION_LOCATIONS.get(row["location"] or "", "場所不明"),
            "url": event_url(row["id"]),
        }
        for row in reversed(records[-RECENT_RECORDS:])
    ]
    return render_template(
        "home.html",
        title=title or APP_TITLE,
        capacity=capacity_info(store.root, disk_mb),
        pending=pending,
        manual_only=manual_only,
        today=today,
        daily=daily,
        food_grams=format_grams(sum(fed)),
        token=token,
        quick_feed=feeding_text(*last) if last else "",
        recorded=_recorded_notice(done, today.isoformat()) if done else None,
        quick=HOME_QUICK,
        recent=recent,
        last_care=_last_care(store, records, today),
        timezone=timezone_label(),
    )


def event_url(event_id: str) -> str:
    """記録を検知の一覧で開く URL（ラベル済みも含めて表示する）。"""
    return f"{EVENTS_PAGE}?" + urlencode({"filter": "all", "id": event_id})


# 時間帯別の棒グラフ。棒の高さは CSS の level-0〜level-10 の段階で示す（CSP でインラインの
# style を使えないため）。軸の目盛りは CHART_TICK 時間ごと、多い時間帯は BUSIEST 件まで挙げる。
CHART_LEVELS = 10
CHART_TICK = 3
BUSIEST = 2
CHART_KINDS = ("feces", "urine")


def _bar_level(count: int, scale: int) -> int:
    """件数を棒の高さの段階にする。1件でもあれば、見える高さ（1）にする。"""
    if count <= 0:
        return 0
    return max(1, round(count * CHART_LEVELS / scale))


def hourly_chart(hourly: Sequence[Mapping[str, int]], scale: int) -> list[dict[str, Any]]:
    """24時間の棒グラフの列。各列に排便・排尿の件数と棒の高さの段階を持つ。"""
    return [
        {
            "hour": hour,
            "label": str(hour) if hour % CHART_TICK == 0 else "",
            "bars": [
                {"kind": kind, "count": counts[kind], "level": _bar_level(counts[kind], scale)}
                for kind in CHART_KINDS
            ],
        }
        for hour, counts in enumerate(hourly)
    ]


def busiest_hours(hourly: Sequence[Mapping[str, int]], kind: str) -> str:
    """指定した種類で件数が多い時間帯を示し、同数なら時刻が早い順に並べる。"""
    counts = [(hour, row[kind]) for hour, row in enumerate(hourly) if row[kind] > 0]
    top = sorted(counts, key=lambda item: (-item[1], item[0]))
    return "、".join(f"{hour}時台（{count}件）" for hour, count in top[:BUSIEST])


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
    scale = max(1, *(counts[kind] for counts in hourly for kind in CHART_KINDS))
    busiest = {EXCRETION_TYPES[kind]: busiest_hours(hourly, kind) for kind in CHART_KINDS}
    return {
        "day": day,
        "period": period,
        "periods": STATS_PERIODS,
        "start": start,
        "length": (day - start).days + 1,
        "daily": daily,
        "hourly": list(enumerate(hourly)),
        "recorded_days": recorded_days,
        "scale": scale,
        "chart": hourly_chart(hourly, scale),
        "busiest": busiest if any(busiest.values()) else {},
    }


def record_row(row: EventRow, videos: Sequence[str]) -> dict[str, Any]:
    start = list_time(row["actual_start"] or row["timestamp"])
    end = list_time(row["actual_end"])
    return {
        "type": EXCRETION_TYPES[row["excretion_type"]],
        "location": EXCRETION_LOCATIONS.get(row["location"] or "", "場所不明"),
        "source": SOURCES[row["kind"]],
        "note": row["note"],
        "icon": RECORD_ICONS.get(row["excretion_type"], "paw"),
        "when": start,
        "end": end,
        # 完了が開始と別の日なら、完了にも日付を付ける。
        "end_clock": bool(start and end and start.day == end.day),
        "start_unknown": row["kind"] == "VIDEO" and not row["actual_start"],
        "videos": videos,
        "url": event_url(row["id"]),
    }


def render_records(
    store: Store, clips: ClipStore, query: Query, *, title: str | None = None
) -> str:
    records = store.records()
    video_links = clips.labels.record_videos()
    return render_template(
        "records.html",
        saved=bool(query.get("saved")),
        title=title or APP_TITLE,
        stats=record_stats(records, query),
        timezone=timezone_label(),
        groups=by_day(record_row(row, video_links.get(row["id"], [])) for row in reversed(records)),
    )
