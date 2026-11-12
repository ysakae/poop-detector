"""ローカル動画の再生と、動作学習用ラベルの入力画面。"""

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..records.events import EXCRETION_LOCATIONS, EXCRETION_TYPES
from ..video.clip_labels import ClipLabel
from ..video.clip_storage import (
    CLEANUP_LIMIT,
    DEFAULT_DISK_MIB,
    MIB,
    RETENTION_DAYS,
    cleanup_candidates,
    cleanup_selected,
    is_protected,
    protect,
    recording_usage,
    retention_days,
)
from ..video.clips import ACTIVITIES, CLIP_LABELS, ClipRow, ClipStore
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
    "outside_stay": "トレー外での滞在",
}


def clip_list_item(item: Mapping[str, Any], selected: str | None) -> dict[str, Any]:
    from .pages import local_time

    label = CLIP_LABELS.get(item["label"], "未ラベル")
    if item["label"] is None:
        if item["review_label"]:
            label = "自動判定の候補 ／ " + CLIP_LABELS[item["review_label"]]
        elif item["review_error"]:
            label = "自動判定の失敗"
    location = item["review_location"] if item["label"] is None else item["location"]
    if location:
        label += " ／ " + EXCRETION_LOCATIONS.get(location, "場所不明")
    return {
        "id": item["id"],
        "active": item["id"] == selected,
        "time": local_time(item["requested_at"]),
        "status": STATES.get(item["status"], item["status"]),
        "label": label,
    }


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
        "duration": row["duration"],
        "trigger_offset": row["trigger_offset"] or 0,
        "start_time": local_time(row["start_at"]),
        "interrupted": row["interrupted"],
        "note": row["note"],
        "event_id": row["event_id"],
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


def render_clips(
    root: str | Path,
    token: str,
    query: Mapping[str, list[str]],
    disk_mb: float = DEFAULT_DISK_MIB,
    *,
    title: str | None = None,
) -> str:
    """動画の一覧と、選んだ動画の再生・ラベル入力欄。"""
    from .pages import APP_TITLE, local_time

    store = ClipStore(root)
    rows = store.list(include_assessments=True)
    selected = query.get("id", [""])[0]
    row = next((r for r in rows if r["id"] == selected), rows[0] if rows else None)
    raw_assessment = store.assessments.get(row["id"]) if row else None
    context: dict[str, Any] = {
        "clip": None,
        "circling": None,
        "assessment": None,
        "form": None,
        "confirmation": None,
    }
    if row is not None:
        form = label_form(row, cast("Mapping[str, Any] | None", raw_assessment))
        context.update(
            clip=clip_detail(store, row),
            circling=circling_detail(store, row) if row["status"] == "ready" else None,
            assessment=assessment_detail(
                cast("Mapping[str, Any] | None", raw_assessment), row["label"] is not None
            ),
            form=form,
            confirmation=confirmation_info(store, row, local_time, form["location"])
            if row["status"] == "ready"
            else None,
        )
    return render_template(
        "clips.html",
        title=title or APP_TITLE,
        token=token,
        capacity=capacity_info(store.root, disk_mb),
        live=store.live(),
        saved=bool(query.get("saved")),
        items=[clip_list_item(item, row["id"] if row else None) for item in rows],
        clip_labels=list(CLIP_LABELS.items()),
        activities=list(ACTIVITIES.items()),
        locations=list(EXCRETION_LOCATIONS.items()),
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
    size = path.stat().st_size
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
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(max(0, end - start + 1)),
        "Cache-Control": "no-store",
    }
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


def handle_clip_post(
    handler: "Exchange",
    root: str | Path,
    fields: Mapping[str, str],
    selected_ids: Iterable[str] = (),
    *,
    title: str | None = None,
) -> None:
    """/clips/ 以下のフォームを処理し、成功したら一覧へ戻す。"""
    store = ClipStore(root)
    try:
        if handler.path == "/manage/storage/cleanup":
            if fields.get("confirm") != "1":
                raise ValueError("削除の確認欄にチェックを入れてください")
            days = retention_days(fields.get("days", ""))
            removed, skipped = cleanup_selected(store, selected_ids, days)
            handler.redirect(f"/manage/storage?days={days}&removed={removed}&skipped={skipped}")
            return
        if handler.path == "/clips/request":
            clip_id = store.request(require_live=True)
        elif handler.path == "/clips/protect":
            clip_id = fields.get("id", "")
            if fields.get("protected") not in ("0", "1"):
                raise ValueError("保護の設定を確認してください")
            protect(store, clip_id, fields["protected"] == "1")
        else:
            clip_id = fields.get("id", "")
            start = float(fields["start"]) if fields.get("start") else None
            end = float(fields["end"]) if fields.get("end") else None
            clip_label = ClipLabel(
                fields.get("label", ""),
                start,
                end,
                fields.get("activity", "other"),
                fields.get("note", ""),
                fields.get("location"),
            )
            record_id = fields.get("record_id", "") if handler.path == "/clips/confirm" else None
            store.labels.save(clip_id, clip_label, record_id=record_id)
        handler.redirect(f"/clips?id={clip_id}&saved=1")
    except (ValueError, TypeError) as error:
        from .pages import message_page

        # ValueError の文言はこのパッケージで書いたもので、テンプレートがエスケープする。
        if handler.path == "/manage/storage/cleanup":
            page = message_page(
                "動画を整理できませんでした",
                str(error),
                "/manage/storage",
                "録画の整理へ戻る",
                title=title,
                wrap_link=True,
            )
            handler.respond(400, page)
            return
        if handler.path == "/clips/confirm":
            page = message_page(
                "確定記録を保存できませんでした",
                str(error),
                "/clips",
                "動画一覧へ戻る",
                title=title,
                wrap_link=True,
            )
            handler.respond(400, page)
            return
        status = 409 if handler.path == "/clips/request" else 400
        page = message_page(
            "保存できませんでした",
            "監視とカメラの接続、保存処理数、動画内の時刻を確認してください。",
            "/clips",
            "動画一覧へ戻る",
            title=title,
        )
        handler.respond(status, page)
        return
