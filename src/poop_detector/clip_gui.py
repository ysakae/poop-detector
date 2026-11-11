"""ローカル動画の再生と、動作学習用ラベルの入力画面。"""

import re
from collections.abc import Callable, Iterable, Mapping
from email.message import Message
from functools import partial
from html import escape
from io import BufferedIOBase
from pathlib import Path
from typing import Any, Protocol, cast

from .clip_storage import (
    CLEANUP_LIMIT,
    DEFAULT_DISK_MIB,
    MIB,
    RETENTION_DAYS,
    cleanup_candidates,
    cleanup_selected,
    recording_usage,
    retention_days,
)
from .clips import ACTIVITIES, CLIP_LABELS, ClipRow, ClipStore
from .events import EXCRETION_LOCATIONS, EXCRETION_TYPES

STATES = {
    "queued": "受付済み",
    "recording": "録画中",
    "encoding": "変換中",
    "ready": "再生可能",
    "failed": "保存失敗",
    "removing": "整理中",
    "removed": "整理済み",
}


class ClipHandler(Protocol):
    """動画の配信とフォームの処理に使う、ラベル GUI のリクエストハンドラーの部分。"""

    path: str
    # 型スタブでは Message は総称型だが、実行時には添字を付けられないため文字列で書く。
    headers: "Message[str, str]"
    wfile: BufferedIOBase

    def send_response(self, code: int, message: str | None = None) -> None: ...
    def send_header(self, keyword: str, value: str) -> None: ...
    def end_headers(self) -> None: ...
    def respond(self, status: int, data: str | bytes) -> None: ...


SOURCES = {
    "manual": "手動保存",
    "detected": "差分検知",
    "dog_enter": "犬の入場",
    "dog_exit": "犬の退場",
    "dog_visit": "犬の入退場",
    "early_alert": "トイレ見守り",
    "outside_stay": "トレー外での滞在",
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
    from .label_gui import document, local_time

    page = partial(document, title=title)

    store = ClipStore(root)
    rows = store.list(include_assessments=True)
    selected = query.get("id", [""])[0]
    row = next((r for r in rows if r["id"] == selected), rows[0] if rows else None)
    assessment = store.assessment(row["id"]) if row else None
    csrf = f'<input type="hidden" name="csrf" value="{token}">'
    content = '<h2>動画を見てラベルを付ける</h2><p><a href="/">ライブ映像・排泄の記録へ戻る</a></p>'
    content += (
        "<p>排泄前後の動作も確認し、排泄なしの動画にもラベルを付けてください。"
        "排便や排尿を確認したら、動画ラベルと確定記録を一緒に保存できます。</p>"
    )
    content += (
        '<form method="post" action="/clips/request">'
        + csrf
        + "<button>今の前後の動画を保存</button></form>"
        '<p class="note">監視起動中に使えます。既定では操作前30秒と操作後30秒を保存します。'
        "録画後の変換が終わったら「更新」を押してください。</p>"
        '<p><a href="/clips">更新</a></p>'
    )
    content += capacity_panel(store.root, disk_mb)
    content += '<p><a href="/clips/storage">録画容量と古い動画の整理</a></p>'
    if not store.live():
        content += '<div class="message">動画収集のカメラ接続を確認できません。監視を起動し、カメラ接続を確認してください。</div>'
    if query.get("saved"):
        content += '<div class="message" role="status">保存しました。</div>'
    content += '<div class="workspace"><nav id="clip-list" aria-label="動画一覧">'
    for item in rows:
        label = CLIP_LABELS.get(item["label"], "未ラベル")
        if item["label"] is None:
            if item["codex_label"]:
                label = "Codex候補 ／ " + CLIP_LABELS[item["codex_label"]]
            elif item["codex_error"]:
                label = "Codex判定失敗"
        location = item["codex_location"] if item["label"] is None else item["location"]
        if location:
            label += " ／ " + EXCRETION_LOCATIONS.get(location, "場所不明")
        status = STATES.get(item["status"], item["status"])
        active = "active" if row and row["id"] == item["id"] else ""
        content += (
            f'<a class="{active}" href="/clips?id={item["id"]}">'
            + escape(local_time(item["requested_at"]))
            + f"<small>{escape(status)} ／ {escape(label)}</small></a>"
        )
    content += '</nav><article id="clip-detail">'
    if row is None:
        content += '<div class="empty"><h3>動画はまだありません</h3><p>監視を起動して保存ボタンを押すか、自動検知を待ってください。</p></div>'
    else:
        content += f"<h3>{escape(local_time(row['requested_at']))}</h3>"
        content += (
            "<p>"
            + SOURCES.get(row["source"], "保存要求")
            + " ／ "
            + STATES.get(row["status"], "")
            + "</p>"
        )
        if row["status"] == "ready":
            pinned = store.is_pinned(row["id"])
            pin_text = "保護指定を解除" if pinned else "この動画を保護"
            content += (
                '<form method="post" action="/clips/protect">'
                + csrf
                + f'<input type="hidden" name="id" value="{escape(row["id"], quote=True)}">'
                + f'<button class="secondary" name="protected" value="{int(not pinned)}">{pin_text}</button></form>'
            )
            content += (
                f'<video controls preload="metadata" style="width:100%;background:#192d39" '
                f'src="/clip-video?id={row["id"]}">動画を再生できません。</video>'
            )
            content += (
                f'<p class="note">長さ {row["duration"]:.1f}秒。操作・検知位置は動画内 '
                f"{row['trigger_offset'] or 0:.1f}秒です。"
                f"保存開始: {escape(local_time(row['start_at']))}</p>"
            )
            if row["interrupted"]:
                content += '<div class="message">切断・停止・フレーム欠落がある部分動画です。欠落区間では同じフレームを表示することがあります。</div>'
            circling = store.circling_review(row["id"])
            if circling is not None:
                content += (
                    '<div class="message"><strong>行動分析 ／ 旋回候補</strong>'
                    f"<p>トレー付近の位置を取得 {circling['located']}/{circling['sampled']}コマ</p>"
                )
                if not circling["candidates"]:
                    content += "<p>旋回候補はありません。</p>"
                for start, end in circling["candidates"]:
                    content += (
                        f'<p><button class="secondary" type="button" data-video-seek="{start}">'
                        f"旋回候補 {start:.1f}〜{end:.1f}秒へ移動</button>"
                    )
                    actual_start = row["actual_start_offset"]
                    if actual_start is not None and actual_start > end:
                        content += f" ／ 排泄の開始記録より {actual_start - end:.1f}秒前に検出"
                    content += "</p>"
                content += "</div>"
            # Codex の判定があり、まだ人がラベルを付けていなければ、入力欄の初期値にする。
            draft: dict[str, Any] = {}
            if assessment:
                if assessment["error"]:
                    content += '<div class="message">' + escape(assessment["error"]) + "</div>"
                else:
                    content += (
                        '<div class="message"><strong>Codex判定候補 ／ '
                        + escape(CLIP_LABELS[cast(str, assessment["label"])])
                        + (
                            f"（{EXCRETION_LOCATIONS[assessment['location']]}）"
                            if assessment["location"] is not None
                            else ""
                        )
                        + " ／ "
                        + escape(ACTIVITIES[cast(str, assessment["activity"])])
                        + "</strong><br>"
                        + escape(assessment["reason"] or "")
                    )
                    for seconds, button_title in (
                        (assessment["start_seconds"], "開始候補へ移動"),
                        (assessment["end_seconds"], "完了候補へ移動"),
                    ):
                        if seconds is not None:
                            content += (
                                f'<p><button class="secondary" type="button" data-video-seek="{seconds}">'
                                f"{button_title}（{seconds:g}秒）</button></p>"
                            )
                    if assessment["notified_at"]:
                        content += "<br>通知済み " + escape(local_time(assessment["notified_at"]))
                    if row["label"] is None:
                        draft = dict(assessment)
                        content += "<br>動画を確認してからラベルを保存してください。"
                    content += "</div>"
            content += (
                '<form method="post" action="/clips/label">'
                + csrf
                + f'<input type="hidden" name="id" value="{row["id"]}">'
                + '<fieldset><legend>動画の内容</legend><div class="choices">'
            )
            for key, label in CLIP_LABELS.items():
                checked = (
                    "checked" if key == (row["label"] or draft.get("label", "uncertain")) else ""
                )
                content += f'<label class="choice"><input type="radio" name="label" value="{key}" {checked}>{label}</label>'
            location = draft.get("location", row["location"])
            content += "</div></fieldset>" + location_field(location)
            content += '<label>動作<select name="activity">'
            for key, activity in ACTIVITIES.items():
                selected_attr = "selected" if key == draft.get("activity", row["activity"]) else ""
                content += f'<option value="{key}" {selected_attr}>{activity}</option>'
            content += (
                '</select></label><p class="note">再生位置を開始・完了として指定できます。'
                '映っていない時刻は空欄にしてください。</p><div class="times">'
            )
            for field, text in [("start", "開始（動画内の秒）"), ("end", "完了（動画内の秒）")]:
                value = row["actual_" + field + "_offset"]
                value = "" if value is None else str(value)
                content += (
                    f'<div><label>{text}<input type="number" min="0" max="{row["duration"]}" '
                    f'step="0.01" name="{field}" value="{value}"></label>'
                    f'<button class="secondary" type="button" data-video-offset="{field}">'
                    f"ここを{'開始' if field == 'start' else '完了'}にする</button></div>"
                )
            content += (
                '</div><p><label>メモ<br><textarea name="note" rows="3" maxlength="2000" style="width:100%">'
                + escape(row["note"])
                + "</textarea></label></p>"
                + '<button class="secondary">動画ラベルのみ保存</button>'
                + confirmation_fields(store, row, local_time, location)
                + "</form>"
            )
            if row["event_id"]:
                content += f'<p><a href="/?filter=all&amp;id={row["event_id"]}">対応するイベント画像を確認</a></p>'
        elif row["status"] in ("removed", "removing"):
            message = (
                "録画ファイルは整理済みです。"
                if row["status"] == "removed"
                else "録画ファイルの整理を再開できます。"
            )
            content += f'<p>{message} <a href="/clips/storage">録画の整理を開く</a></p>'
            if assessment and assessment["reason"]:
                label = CLIP_LABELS.get(assessment["label"] or "", "判定結果")
                content += f"<p>{escape(label)} ／ {escape(assessment['reason'])}</p>"
        elif row["status"] == "failed":
            content += "<p>" + escape(row["error"] or "動画を保存できませんでした") + "</p>"
        else:
            content += "<p>保存処理中です。録画と変換が終わったら更新してください。</p>"
    return page(content + '</article></div><script src="/assets/clips.js" defer></script>')


def capacity_status(root: Path, disk_mb: float) -> str:
    """録画容量の使用量と、上限に近づいたときの警告。"""
    used = recording_usage(root)
    capacity = disk_mb * MIB
    percentage = 100 * used / capacity
    remaining = max(0, capacity - used)
    content = (
        f"<p>録画容量 {used / MIB:,.1f} / {disk_mb:,} MiB（{percentage:.1f}％）"
        f" ／ 上限まで残り {remaining / MIB:,.1f} MiB</p>"
    )
    if percentage >= 100:
        warning = "録画容量の上限に達しています。空きを確保するまで新しい録画は保存できません。"
    elif percentage >= 90:
        warning = "録画容量が90％に達しました。古い動画の整理をご検討ください。"
    elif percentage >= 75:
        warning = "録画容量が75％に達しました。"
    else:
        return content
    return content + f'<p class="capacity-warning" role="status">{warning}</p>'


def capacity_panel(root: Path, disk_mb: float) -> str:
    return (
        '<section id="recording-capacity" aria-label="録画容量">'
        + capacity_status(root, disk_mb)
        + '</section><script src="/assets/clip_storage.js" defer></script>'
    )


def render_storage(
    store: ClipStore,
    token: str,
    query: Mapping[str, list[str]],
    disk_mb: float,
    *,
    title: str | None = None,
) -> str:
    """録画容量と、整理してよい古い動画の選択画面。"""
    from .label_gui import document, local_time

    page = partial(document, title=title)

    try:
        days = retention_days(query.get("days", ["7"])[0])
    except ValueError:
        days = 7
    candidates = cleanup_candidates(store, days)
    options = "".join(
        f'<option value="{day}"{" selected" if day == days else ""}>{day}日以上前</option>'
        for day in RETENTION_DAYS
    )
    content = '<h2>録画容量と古い動画の整理</h2><p><a href="/clips">動画一覧へ戻る</a></p>'
    content += capacity_panel(store.root, disk_mb)
    try:
        removed, skipped = (int(query[key][0]) for key in ("removed", "skipped"))
    except (KeyError, ValueError):
        pass
    else:
        content += f'<p class="message" role="status">{removed}本を整理しました。{skipped}本は対象外または再試行が必要です。</p>'
    content += (
        '<form method="get" action="/clips/storage"><label>整理候補の古さ'
        f'<select name="days">{options}</select></label> <button>候補を表示</button></form>'
        '<p class="note">手動ラベル付き、検知や確定記録にひも付いた動画、保護指定した動画、'
        "Codexが排泄候補と判定した動画は整理対象から外れます。</p>"
    )
    if not candidates:
        return page(content + "<p>整理できる古い動画はありません。</p>")
    total = sum(size for _, size in candidates)
    content += (
        f"<p>表示中の候補 {len(candidates)}本（古い順に最大{CLEANUP_LIMIT}本）"
        f" ／ すべて選んだ場合に空く容量の目安 {total / MIB:,.2f} MiB</p>"
        '<form id="cleanup-form" method="post" action="/clips/cleanup">'
        f'<input type="hidden" name="csrf" value="{token}">'
        f'<input type="hidden" name="days" value="{days}">'
        '<p><label><input type="checkbox" id="cleanup-all">表示中の候補をすべて選択</label></p>'
        "<fieldset><legend>整理する動画を選択</legend>"
    )
    for row, size in candidates:
        clip_id = escape(row["id"], quote=True)
        stamp = escape(local_time(row["requested_at"]))
        source = SOURCES.get(row["source"], "動画")
        verdict = CLIP_LABELS.get(row["codex_label"], "未判定")
        content += (
            f'<p><label><input type="checkbox" name="id" value="{clip_id}" data-bytes="{size}">'
            f"{stamp} ／ {source} ／ {verdict}</label>"
            f' ／ {size / MIB:,.2f} MiB ／ <a href="/clips?id={clip_id}" target="_blank" rel="noopener">確認</a></p>'
        )
    content += (
        '</fieldset><p id="cleanup-selection" role="status">選択済み 0本 ／ 空く容量の目安 0.00 MiB</p>'
        '<p><label><input type="checkbox" name="confirm" value="1" required>'
        "選んだ動画ファイルを削除することを確認しました</label></p>"
        "<button>選択した動画ファイルを削除</button></form>"
        '<p class="note">削除した映像は再生できなくなります。判定結果や記録は残ります。</p>'
    )
    return page(content)


def location_field(location: str | None) -> str:
    content = '<label>排泄した場所<select name="location">'
    for value, title in (("", "場所不明"), *EXCRETION_LOCATIONS.items()):
        selected = " selected" if value == (location or "") else ""
        content += f'<option value="{value}"{selected}>{title}</option>'
    return content + "</select></label>"


def confirmation_fields(
    store: ClipStore,
    clip: Mapping[str, Any],
    local_time: Callable[[str | None], str],
    location: str | None,
) -> str:
    """動画のラベルと一緒に、排泄の確定記録を保存する欄。保存先の記録を選ばせる。"""
    linked = store.confirmation(clip["id"])
    content = "<fieldset><legend>排便・排尿の確定記録</legend>"
    if linked:
        content += (
            f'<input type="hidden" name="record_id" value="{escape(linked, quote=True)}">'
            f'<p><a href="/?filter=all&amp;id={escape(linked, quote=True)}">ひも付け済みの記録を開く</a></p>'
            '<p class="note">確定した日時や種類は、ひも付け済みの記録から編集できます。</p>'
        )
    else:
        candidates = store.record_candidates(cast(ClipRow, clip))
        original = clip["event_id"]
        content += '<label>保存先<select name="record_id">'
        if candidates:
            content += '<option value="">保存先を選択</option>'
        for record in candidates:
            is_original = record["id"] == original
            known_location = record["location"] or ("tray" if record["kind"] == "SUSPECTED" else "")
            disabled = (
                " disabled" if location and known_location and location != known_location else ""
            )
            selected = " selected" if is_original and not disabled else ""
            label = EXCRETION_TYPES[record["excretion_type"]] if record["feedback"] == 1 else "検知"
            if known_location:
                label += "・" + EXCRETION_LOCATIONS[known_location]
            stamp = local_time(record["actual_start"] or record["timestamp"])
            content += (
                f'<option value="{escape(record["id"], quote=True)}" data-record-location="{known_location}"'
                f"{selected}{disabled}>{escape(stamp)} {escape(label)}</option>"
            )
        disabled = " disabled" if original and location != "outside" else ""
        content += f'<option value="new" data-tray-event="{int(bool(original))}"{disabled}>新しい確定記録を作成</option>'
        content += '</select></label><p class="note">同じ排泄がすでに記録されている場合は、その記録を選んでください。</p>'
    return (
        content + '<button formaction="/clips/confirm">ラベルと確定記録を保存</button></fieldset>'
    )


def stream_video(handler: ClipHandler, root: str | Path, clip_id: str) -> None:
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
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return None
    handler.send_response(206 if range_header else 200)
    handler.send_header("Content-Type", "video/mp4")
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(max(0, end - start + 1)))
    handler.send_header("Cache-Control", "no-store")
    if range_header:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()
    try:
        with path.open("rb") as video:
            video.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                block = video.read(min(65536, remaining))
                if not block:
                    break
                handler.wfile.write(block)
                remaining -= len(block)
    except (BrokenPipeError, ConnectionResetError):
        pass


def handle_clip_post(
    handler: ClipHandler,
    root: str | Path,
    fields: Mapping[str, str],
    selected_ids: Iterable[str] = (),
    *,
    title: str | None = None,
) -> None:
    """/clips/ 以下のフォームを処理し、成功したら一覧へ戻す。"""
    store = ClipStore(root)
    try:
        if handler.path == "/clips/cleanup":
            if fields.get("confirm") != "1":
                raise ValueError("削除の確認欄にチェックを入れてください")
            days = retention_days(fields.get("days", ""))
            removed, skipped = cleanup_selected(store, selected_ids, days)
            handler.send_response(303)
            handler.send_header(
                "Location", f"/clips/storage?days={days}&removed={removed}&skipped={skipped}"
            )
            handler.end_headers()
            return None
        if handler.path == "/clips/request":
            clip_id = store.request(require_live=True)
        elif handler.path == "/clips/protect":
            clip_id = fields.get("id", "")
            if fields.get("protected") not in ("0", "1"):
                raise ValueError("保護の設定を確認してください")
            store.pin(clip_id, fields["protected"] == "1")
        else:
            clip_id = fields.get("id", "")
            start = float(fields["start"]) if fields.get("start") else None
            end = float(fields["end"]) if fields.get("end") else None
            store.label(
                clip_id,
                fields.get("label", ""),
                start,
                end,
                fields.get("activity", "other"),
                fields.get("note", ""),
                record_id=fields.get("record_id", "") if handler.path == "/clips/confirm" else None,
                location=fields.get("location"),
            )
        handler.send_response(303)
        handler.send_header("Location", f"/clips?id={clip_id}&saved=1")
        handler.end_headers()
    except (ValueError, TypeError) as error:
        from .label_gui import document

        page = partial(document, title=title)

        if handler.path == "/clips/cleanup":
            return handler.respond(
                400,
                page(
                    f"<h2>動画を整理できませんでした</h2><p>{escape(str(error))}</p>"
                    '<p><a href="/clips/storage">録画の整理へ戻る</a></p>'
                ),
            )
        if handler.path == "/clips/confirm":
            return handler.respond(
                400,
                page(
                    f"<h2>確定記録を保存できませんでした</h2><p>{escape(str(error))}</p>"
                    '<p><a href="/clips">動画一覧へ戻る</a></p>'
                ),
            )

        status = 409 if handler.path == "/clips/request" else 400
        handler.respond(
            status,
            page(
                '<h2>保存できませんでした</h2><p>監視とカメラの接続、保存処理数、動画内の時刻を確認してください。</p><a href="/clips">動画一覧へ戻る</a>'
            ),
        )
