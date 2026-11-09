"""ローカル動画の再生と、動作学習用ラベルの入力画面。"""

import re
from html import escape

from .clips import ACTIVITIES, CLIP_LABELS, ClipStore
from .events import EXCRETION_LOCATIONS, EXCRETION_TYPES

STATES = {
    "queued": "受付済み",
    "recording": "録画中",
    "encoding": "変換中",
    "ready": "再生可能",
    "failed": "保存失敗",
}
SOURCES = {
    "manual": "手動保存",
    "detected": "差分検知",
    "dog_enter": "犬の入場",
    "dog_exit": "犬の退場",
    "early_alert": "トイレ見守り",
    "outside_stay": "トレー外での滞在",
}


def render_clips(root, token, query):
    from .label_gui import document, local_time

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
            draft = {}
            if assessment:
                if assessment["error"]:
                    content += '<div class="message">' + escape(assessment["error"]) + "</div>"
                else:
                    content += (
                        '<div class="message"><strong>Codex判定候補 ／ '
                        + escape(CLIP_LABELS[assessment["label"]])
                        + (
                            f"（{EXCRETION_LOCATIONS[assessment['location']]}）"
                            if assessment["location"]
                            else ""
                        )
                        + " ／ "
                        + escape(ACTIVITIES[assessment["activity"]])
                        + "</strong><br>"
                        + escape(assessment["reason"])
                    )
                    for field, title in (
                        ("start_seconds", "開始候補へ移動"),
                        ("end_seconds", "完了候補へ移動"),
                    ):
                        seconds = assessment[field]
                        if seconds is not None:
                            content += (
                                f'<p><button class="secondary" type="button" data-video-seek="{seconds}">'
                                f"{title}（{seconds:g}秒）</button></p>"
                            )
                    if assessment["notified_at"]:
                        content += "<br>通知済み " + escape(local_time(assessment["notified_at"]))
                    if row["label"] is None:
                        draft = assessment
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
        elif row["status"] == "failed":
            content += "<p>" + escape(row["error"] or "動画を保存できませんでした") + "</p>"
        else:
            content += "<p>保存処理中です。録画と変換が終わったら更新してください。</p>"
    return document(content + '</article></div><script src="/assets/clips.js" defer></script>')


def location_field(location):
    content = '<label>排泄した場所<select name="location">'
    for value, title in (("", "場所不明"), *EXCRETION_LOCATIONS.items()):
        selected = " selected" if value == (location or "") else ""
        content += f'<option value="{value}"{selected}>{title}</option>'
    return content + "</select></label>"


def confirmation_fields(store, clip, local_time, location):
    linked = store.confirmation(clip["id"])
    content = "<fieldset><legend>排便・排尿の確定記録</legend>"
    if linked:
        content += (
            f'<input type="hidden" name="record_id" value="{escape(linked, quote=True)}">'
            f'<p><a href="/?filter=all&amp;id={escape(linked, quote=True)}">ひも付け済みの記録を開く</a></p>'
            '<p class="note">確定した日時や種類は、ひも付け済みの記録から編集できます。</p>'
        )
    else:
        candidates = store.record_candidates(clip)
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


def stream_video(handler, root, clip_id):
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
            return
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


def handle_clip_post(handler, root, fields):
    store = ClipStore(root)
    try:
        if handler.path == "/clips/request":
            clip_id = store.request(require_live=True)
        else:
            clip_id = fields.get("id", "")
            start = float(fields["start"]) if fields.get("start") else None
            end = float(fields["end"]) if fields.get("end") else None
            store.label(
                clip_id,
                fields.get("label"),
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

        if handler.path == "/clips/confirm":
            return handler.respond(
                400,
                document(
                    f"<h2>確定記録を保存できませんでした</h2><p>{escape(str(error))}</p>"
                    '<p><a href="/clips">動画一覧へ戻る</a></p>'
                ),
            )

        status = 409 if handler.path == "/clips/request" else 400
        handler.respond(
            status,
            document(
                '<h2>保存できませんでした</h2><p>監視とカメラの接続、保存処理数、動画内の時刻を確認してください。</p><a href="/clips">動画一覧へ戻る</a>'
            ),
        )
