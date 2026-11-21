"""ローカル動画の再生と、動作学習用ラベルの入力画面。"""

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

from ..records.events import EXCRETION_LOCATIONS, EXCRETION_TYPES
from ..video.benchmark_candidates import CONDITION_TAGS, EVALUATED_LABELS, Candidate
from ..video.benchmark_candidates import STATES as CANDIDATE_STATES
from ..video.clip_storage import (
    CLEANUP_LIMIT,
    DEFAULT_DISK_MIB,
    MIB,
    RETENTION_DAYS,
    cleanup_candidates,
    is_protected,
    recording_usage,
    retention_days,
)
from ..video.clips import ACTIVITIES, CLIP_LABELS, PENDING_STATUSES, ClipRow, ClipStore
from ..video.review import RESUME_WINDOW
from .assets import VIDEO_CACHE
from .list_time import by_day, list_time
from .review_queue import ReviewItem, review_bar
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange

STATES = {
    "queued": "受付済み",
    "recording": "録画中",
    "encoding": "変換中",
    "ready": "再生可能",
    "failed": "保存失敗",
    "removing": "整理中",
    "removed": "整理済み",
}


SOURCES = {
    "manual": "手動保存",
    "detected": "差分検知",
    "dog_enter": "犬の入場",
    "dog_exit": "犬の退場",
    "dog_visit": "犬の入退場",
    "early_alert": "トイレ見守り",
    "toilet_reentry": "排泄後の再侵入",
    "outside_stay": "トレー外での滞在",
}
# 一覧の絞り込み（?show=）。値は、動画の行がその条件に入るかを返す。
CLIP_FILTERS: dict[str, tuple[str, Callable[[Mapping[str, Any]], bool]]] = {
    "all": ("すべて", lambda _row: True),
    "unlabeled": ("未ラベル", lambda row: row["label"] is None),
    "labeled": ("ラベル済み", lambda row: row["label"] is not None),
    # 画像の差分で見つけた検知の動画（以前の「検知の一覧」の代わり）。
    "detected": ("差分検知", lambda row: row["source"] == "detected"),
}


def clips_url(mode: str, clip_id: str | None = None, **extra: str) -> str:
    """動画の画面の URL。絞り込みが「すべて」のときは付けない。extra は後ろに足す値。"""
    values = ({"id": clip_id} if clip_id else {}) | ({"show": mode} if mode != "all" else {})
    values |= extra
    return "/clips" + ("?" + urlencode(values) if values else "")


def kept_filter(fields: Mapping[str, str]) -> str:
    """フォームが持ち帰った絞り込み。知らない値は「すべて」にして、転送先に付けない。"""
    mode = fields.get("show", "all")
    return mode if mode in CLIP_FILTERS else "all"


def clip_filters(rows: Sequence[Mapping[str, Any]], mode: str) -> list[dict[str, Any]]:
    """絞り込みのボタン。それぞれの条件に入る本数を添える。"""
    return [
        {
            "text": f"{text} {sum(1 for row in rows if shown(row))}",
            "url": clips_url(value),
            "current": value == mode,
        }
        for value, (text, shown) in CLIP_FILTERS.items()
    ]


def clip_assessment_summary(label: str | None, location: str | None, error: bool) -> str:
    """動画一覧と確認待ちで使う自動判定の短い結果。"""
    if label is None:
        return "失敗" if error else ""
    result = CLIP_LABELS[label]
    if label in EXCRETION_TYPES:
        result += "の候補"
    if location:
        result += " ／ " + EXCRETION_LOCATIONS.get(location, "場所不明")
    return result


def clip_list_item(
    item: Mapping[str, Any],
    selected: str | None,
    candidate: Candidate | None = None,
    mode: str = "all",
    *,
    review_waiting: bool = False,
) -> dict[str, Any]:
    label = CLIP_LABELS.get(item["label"], "")
    if label and item["location"]:
        label += " ／ " + EXCRETION_LOCATIONS.get(item["location"], "場所不明")
    return {
        "id": item["id"],
        "url": clips_url(mode, item["id"]),
        "active": item["id"] == selected,
        "when": list_time(item["requested_at"]),
        "status": STATES.get(item["status"], item["status"]),
        "label": label,
        "assessment": clip_assessment_summary(
            item["review_label"], item["review_location"], bool(item["review_error"])
        ),
        "review_waiting": review_waiting,
        "candidate": CANDIDATE_STATES[candidate.state]
        if candidate is not None and candidate.state is not None
        else "",
        "thumbnail": item["status"] == "ready" and bool(item["path"]),
        **pending_marks(item["status"]),
    }


def pending_marks(status: str) -> dict[str, Any]:
    """変換待ちかどうかと、状態の値（static/clip_status.js が変化を見比べる）。"""
    return {"pending": status in PENDING_STATUSES, "status_key": status}


def tools_alert(live: bool, capacity: Mapping[str, Any]) -> str:
    """たたんだ「動画を保存する・録画容量」の見出しに出す、開かなくても伝えたい注意。"""
    alerts = [] if live else ["カメラ接続を確認できません"]
    if capacity["percentage"] >= 90:
        alerts.append("録画容量に注意")
    return "・".join(alerts)


def clip_detail(store: ClipStore, row: Mapping[str, Any]) -> dict[str, Any]:
    """選んだ動画の状態。テンプレートは ready・removed・failed で表示を分ける。"""
    from .pages import local_time

    status = row["status"]
    return {
        "id": row["id"],
        "time": local_time(row["requested_at"]),
        "source": SOURCES.get(row["source"], "保存要求"),
        "status": STATES.get(status, ""),
        "ready": status == "ready",
        "removed": (status == "removed") if status in ("removed", "removing") else None,
        "failed": status == "failed",
        "error": row["error"] or "動画を保存できませんでした",
        "pinned": status == "ready" and is_protected(store, row["id"]),
        "dismissed": status == "ready"
        and row["label"] is None
        and bool(row.get("review_dismissed")),
        "duration": row["duration"],
        "trigger_offset": row["trigger_offset"] or 0,
        "start_time": local_time(row["start_at"]),
        "interrupted": row["interrupted"],
        "note": row["note"],
        "event_id": row["event_id"],
        **pending_marks(status),
    }


def circling_detail(store: ClipStore, row: Mapping[str, Any]) -> dict[str, Any] | None:
    """保存済みの旋回候補。排泄の開始記録より前なら、その差も出す。"""
    circling = store.circling.get(row["id"])
    if circling is None:
        return None
    actual_start = row["actual_start_offset"]
    return {
        "located": circling["located"],
        "sampled": circling["sampled"],
        "candidates": [
            {
                "start": start,
                "end": end,
                "lead": actual_start - end
                if actual_start is not None and actual_start > end
                else None,
            }
            for start, end in circling["candidates"]
        ],
    }


def assessment_detail(assessment: Mapping[str, Any] | None, labeled: bool) -> dict[str, Any] | None:
    """自動判定。人がまだラベルを付けていなければ、入力欄の初期値（draft）にする。"""
    from .pages import local_time

    if not assessment:
        return None
    label = assessment["label"]
    detail: dict[str, Any] = {
        "error": assessment["error"],
        "reason": assessment["reason"] or "",
        "label_or_default": CLIP_LABELS.get(label or "", "判定結果"),
        "draft": False,
    }
    if assessment["error"]:
        return detail
    detail.update(
        label=CLIP_LABELS[cast(str, label)],
        location=EXCRETION_LOCATIONS[assessment["location"]]
        if assessment["location"] is not None
        else "",
        activity=ACTIVITIES[cast(str, assessment["activity"])],
        seeks=[
            {"value": seconds, "text": text, "shown": f"{seconds:g}"}
            for seconds, text in (
                (assessment["start_seconds"], "開始候補へ移動"),
                (assessment["end_seconds"], "完了候補へ移動"),
            )
            if seconds is not None
        ],
        notified=local_time(assessment["notified_at"]) if assessment["notified_at"] else "",
        draft=not labeled,
    )
    return detail


def benchmark_form(
    store: ClipStore, row: Mapping[str, Any], candidate: Candidate | None
) -> dict[str, Any] | None:
    """評価の候補とタグの入力欄。保存済みの動画だけに出し、ラベルがなければ案内だけにする。"""
    if row["status"] != "ready":
        return None
    if row["label"] not in EVALUATED_LABELS:
        return {"available": False}
    tags = candidate.tags if candidate else ()
    if store.labels.confirmation(row["id"]):
        linked = "確定記録"
    elif row["event_id"]:
        linked = "検知"
    else:
        linked = None
    return {
        "available": True,
        "state": (candidate.state if candidate else None) or "",
        "states": [("", "選ばない"), *CANDIDATE_STATES.items()],
        "presets": [{"tag": tag, "checked": tag in tags} for tag in CONDITION_TAGS],
        "other": "、".join(tag for tag in tags if tag not in CONDITION_TAGS),
        "linked": linked,
    }


def label_form(row: Mapping[str, Any], assessment: Mapping[str, Any] | None) -> dict[str, Any]:
    """動画のラベル入力欄の初期値。自動判定が下書きなら、その値を使う。"""
    draft: Mapping[str, Any] = (
        assessment if assessment and not assessment["error"] and row["label"] is None else {}
    )
    offsets = []
    for name, text, button in [
        ("start", "開始（動画内の秒）", "開始"),
        ("end", "完了（動画内の秒）", "完了"),
    ]:
        value = row["actual_" + name + "_offset"]
        offsets.append((name, text, "" if value is None else str(value), button))
    return {
        "label": row["label"] or draft.get("label", "uncertain"),
        "location": draft.get("location", row["location"]),
        "activity": draft.get("activity", row["activity"]),
        "offsets": offsets,
    }


# 1回押して確定するボタン。(値, 名前, アイコン)。
QUICK_LABELS: tuple[tuple[str, str, str], ...] = (
    ("feces", "排便", "feces"),
    ("urine", "排尿", "urine"),
    ("both", "両方", "paw"),
    ("none", "排泄なし", "none"),
)


def quick_form(
    row: Mapping[str, Any], confirmation: Mapping[str, Any] | None, suggested: str
) -> dict[str, Any] | None:
    """まだ確かめていない変換済みの動画に出す、1回押して確定するボタン。

    自動判定の候補（suggested）と同じ種類だけを塗りのボタンにする。ラベル済み・記録に
    ひも付け済みの動画には出さない（/clips/quick も上書きしない）。
    """
    if confirmation is None or confirmation["linked"] or row["label"] is not None:
        return None
    return {
        "buttons": [
            {"value": value, "text": text, "icon": icon, "main": value == suggested}
            for value, text, icon in QUICK_LABELS
        ]
    }


def render_clips(
    root: str | Path,
    token: str,
    query: Mapping[str, list[str]],
    disk_mb: float = DEFAULT_DISK_MIB,
    *,
    title: str | None = None,
    queue: Sequence[ReviewItem] = (),
    focused: bool = False,
    review_enabled: bool = False,
) -> str:
    """動画の一覧と、選んだ動画の再生・ラベル入力欄。

    queue は確認待ちの列。選んだ動画が列にあれば、どこから開いても残りの件数の帯を出し、
    保存すると次の件へ進む。focused は確認待ちから開いたとき（一覧を出さず、その件だけを見せる）。
    """
    from .pages import APP_TITLE, local_time

    store = ClipStore(root)
    rows = store.list(include_assessments=True)
    selected = query.get("id", [""])[0]
    notice = bool(query.get("notice")) and bool(selected)
    mode = query.get("show", ["all"])[0]
    if mode not in CLIP_FILTERS:
        mode = "all"
    listed = [r for r in rows if CLIP_FILTERS[mode][1](r)]
    # ID で指定した動画は、絞り込みの外でも開く。指定がなければ、絞り込んだ一覧の先頭。
    row = next(
        (r for r in rows if r["id"] == selected), listed[0] if listed and not notice else None
    )
    raw_assessment = store.assessments.get(row["id"]) if row else None
    context: dict[str, Any] = {
        "clip": None,
        "circling": None,
        "assessment": None,
        "form": None,
        "confirmation": None,
        "benchmark": None,
        "quick": None,
    }
    candidates = store.benchmark.all()
    key = f"clip:{row['id'] if row else selected}"
    review = review_bar(queue, key) if focused or any(item.key == key for item in queue) else None
    if row is not None:
        form = label_form(row, cast("Mapping[str, Any] | None", raw_assessment))
        context.update(
            clip=clip_detail(store, row),
            notice_unavailable=notice
            and row["status"] not in PENDING_STATUSES
            and store.video_path(cast(ClipRow, row)) is None,
            circling=circling_detail(store, row) if row["status"] == "ready" else None,
            assessment=assessment_detail(
                cast("Mapping[str, Any] | None", raw_assessment), row["label"] is not None
            ),
            form=form,
            confirmation=confirmation_info(store, row, local_time, form["location"])
            if row["status"] == "ready"
            else None,
            benchmark=benchmark_form(store, row, candidates.get(row["id"])),
        )
        context["quick"] = quick_form(row, context["confirmation"], form["label"])
    start = (
        raw_assessment["start_seconds"] if raw_assessment and not raw_assessment["error"] else None
    )
    seek = (
        f"#t={start:g}"
        if notice and start is not None and row and 0 <= start <= (row["duration"] or 0)
        else ""
    )
    capacity = capacity_info(store.root, disk_mb)
    live = store.live()
    review_waiting = (
        {
            clip["id"]
            for clip in store.assessments.pending(
                len(rows), since=(datetime.now(UTC) - RESUME_WINDOW).isoformat()
            )
        }
        if review_enabled and listed and not focused
        else set()
    )
    items = [
        clip_list_item(
            item,
            row["id"] if row else None,
            candidates.get(item["id"]),
            mode,
            review_waiting=item["id"] in review_waiting,
        )
        for item in listed
    ]
    has_pending = any(item["pending"] for item in items) or bool(
        context["clip"] and context["clip"]["pending"]
    )
    return render_template(
        "clips.html",
        title=title or APP_TITLE,
        token=token,
        capacity=capacity,
        live=live,
        tools_alert=tools_alert(live, capacity),
        has_pending=has_pending,
        refresh_url=clips_url(mode, row["id"] if row else None),
        saved=bool(query.get("saved")),
        benchmark_saved=bool(query.get("benchmark_saved")),
        groups=by_day(items),
        filters=clip_filters(rows, mode),
        show=mode,
        any_clips=bool(rows),
        waiting=len(queue),
        clip_labels=list(CLIP_LABELS.items()),
        activities=list(ACTIVITIES.items()),
        locations=list(EXCRETION_LOCATIONS.items()),
        review=review,
        focused=focused,
        notice=notice,
        seek=seek,
        **context,
    )


def capacity_info(root: Path, disk_mb: float) -> dict[str, Any]:
    """録画容量の使用量と、上限に近づいたときの警告。"""
    used = recording_usage(root)
    capacity = disk_mb * MIB
    percentage = 100 * used / capacity
    if percentage >= 100:
        warning = "録画容量の上限に達しています。空きを確保するまで新しい録画は保存できません。"
    elif percentage >= 90:
        warning = "録画容量が90％に達しました。古い動画の整理をご検討ください。"
    elif percentage >= 75:
        warning = "録画容量が75％に達しました。"
    else:
        warning = ""
    return {
        "used_mib": used / MIB,
        "disk_mb": disk_mb,
        "percentage": percentage,
        "remaining_mib": max(0, capacity - used) / MIB,
        "warning": warning,
    }


def capacity_status(root: Path, disk_mb: float) -> str:
    """録画容量の表示だけの HTML（/clips/capacity）。"""
    return render_template("capacity.html", capacity=capacity_info(root, disk_mb)).strip()


def render_storage(
    store: ClipStore,
    token: str,
    query: Mapping[str, list[str]],
    disk_mb: float,
    *,
    title: str | None = None,
) -> str:
    """録画容量と、整理してよい古い動画の選択画面。"""
    from .pages import APP_TITLE, local_time

    try:
        days = retention_days(query.get("days", ["7"])[0])
    except ValueError:
        days = 7
    candidates = cleanup_candidates(store, days)
    try:
        removed, skipped = (int(query[key][0]) for key in ("removed", "skipped"))
    except (KeyError, ValueError):
        result = None
    else:
        result = {"removed": removed, "skipped": skipped}
    return render_template(
        "storage.html",
        title=title or APP_TITLE,
        token=token,
        capacity=capacity_info(store.root, disk_mb),
        result=result,
        days=days,
        retention_days=RETENTION_DAYS,
        limit=CLEANUP_LIMIT,
        total_mib=sum(size for _, size in candidates) / MIB,
        candidates=[
            {
                "id": row["id"],
                "size": size,
                "size_mib": size / MIB,
                "time": local_time(row["requested_at"]),
                "source": SOURCES.get(row["source"], "動画"),
                "verdict": CLIP_LABELS.get(row["review_label"], "未判定"),
            }
            for row, size in candidates
        ],
    )


def confirmation_info(
    store: ClipStore,
    clip: Mapping[str, Any],
    local_time: Callable[[str | None], str],
    location: str | None,
) -> dict[str, Any]:
    """動画のラベルと一緒に、排泄の確定記録を保存する欄。保存先の記録を選ばせる。"""
    linked = store.labels.confirmation(clip["id"])
    if linked:
        return {"linked": linked}
    original = clip["event_id"]
    candidates = []
    for record in store.labels.record_candidates(cast(ClipRow, clip)):
        known_location = record["location"] or ("tray" if record["kind"] == "SUSPECTED" else "")
        disabled = bool(location and known_location and location != known_location)
        label = EXCRETION_TYPES[record["excretion_type"]] if record["feedback"] == 1 else "検知"
        if known_location:
            label += "・" + EXCRETION_LOCATIONS[known_location]
        candidates.append(
            {
                "id": record["id"],
                "location": known_location,
                "selected": record["id"] == original and not disabled,
                "disabled": disabled,
                "time": local_time(record["actual_start"] or record["timestamp"]),
                "label": label,
            }
        )
    return {
        "linked": None,
        "candidates": candidates,
        "tray_event": bool(original),
        "new_disabled": bool(original and location != "outside"),
    }


def stream_video(handler: "Exchange", root: str | Path, clip_id: str) -> None:
    """動画を Range 要求に対応して返す。"""
    store = ClipStore(root)
    path = store.video_path(store.get(clip_id))
    if path is None:
        return handler.respond(404, "動画が見つかりません")
    stat = path.stat()
    size = stat.st_size
    # 同じ録画を見返すときに取り直さないよう、端末に預けてもらう。書き換わったら（大きさか
    # 更新時刻が変われば）別の ETag になるので、古い中身は使われない。
    tag = f'"{size:x}-{stat.st_mtime_ns:x}"'
    cache = {"Cache-Control": VIDEO_CACHE, "ETag": tag, "Accept-Ranges": "bytes"}
    if handler.headers.get("If-None-Match") == tag:
        handler.respond(304, b"", "video/mp4", cache)
        return None
    start, end = 0, size - 1
    range_header = handler.headers.get("Range")
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
        try:
            if not match or not any(match.groups()):
                raise ValueError
            left, right = match.groups()
            if left:
                start = int(left)
                end = min(int(right), size - 1) if right else size - 1
            else:
                suffix = int(right)
                if suffix <= 0:
                    raise ValueError
                start = max(0, size - suffix)
            if not 0 <= start <= end < size:
                raise ValueError
        except ValueError:
            handler.respond(416, b"", "video/mp4", {"Content-Range": f"bytes */{size}"})
            return None
    headers = {**cache, "Content-Length": str(max(0, end - start + 1))}
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    handler.stream(206 if range_header else 200, read_range(path, start, end), "video/mp4", headers)
    return None


def read_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    """ファイルの start〜end バイト目を少しずつ読む。接続が切れれば読むのをやめる。"""
    with path.open("rb") as video:
        video.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            block = video.read(min(65536, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def handle_benchmark_post(
    handler: "Exchange",
    store: ClipStore,
    fields: Mapping[str, str],
    tags: Iterable[str],
    *,
    title: str | None = None,
) -> None:
    """動画の画面の「評価の候補」のフォームを保存し、その動画へ戻す。"""
    from .pages import message_page

    clip_id = fields.get("id", "")
    try:
        store.benchmark.save(
            clip_id, fields.get("state") or None, [*tags, fields.get("other_tags", "")]
        )
    except ValueError as error:
        # ValueError の文言はこのパッケージで書いたもので、テンプレートがエスケープする。
        page = message_page(
            "評価の候補を保存できませんでした",
            str(error),
            "/clips",
            "動画一覧へ戻る",
            title=title,
            wrap_link=True,
        )
        handler.respond(400, page)
        return
    handler.redirect(clips_url(kept_filter(fields), clip_id, benchmark_saved="1"))
