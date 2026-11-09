"""ローカル動画の再生と、動作学習用ラベルの入力画面。"""

import re
from html import escape

from .clips import ACTIVITIES, CLIP_LABELS, ClipStore

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
}


def render_clips(root, token, query):
    from .label_gui import document, local_time

    store = ClipStore(root)
    rows = store.list(include_assessments=True)
    selected = query.get("id", [""])[0]
    row = next((r for r in rows if r["id"] == selected), rows[0] if rows else None)
    assessment = store.assessment(row["id"]) if row else None
    csrf = f'<input type="hidden" name="csrf" value="{token}">'
    content = '<h2>動画を見てラベルを付ける</h2><p><a href="/">排泄の記録へ戻る</a></p>'
    content += (
        "<p>排泄前後の動作も確認し、排泄なしの動画にもラベルを付けてください。"
        "動画ラベルは学習用で、排泄記録のラベルとは別に保存します。</p>"
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
    content += '<div class="workspace"><nav aria-label="動画一覧">'
    for item in rows:
        label = CLIP_LABELS.get(item["label"], "未ラベル")
        if item["label"] is None:
            if item["codex_label"]:
                label = "Codex候補 ／ " + CLIP_LABELS[item["codex_label"]]
            elif item["codex_error"]:
                label = "Codex判定失敗"
        status = STATES.get(item["status"], item["status"])
        active = "active" if row and row["id"] == item["id"] else ""
        content += (
            f'<a class="{active}" href="/clips?id={item["id"]}">'
            + escape(local_time(item["requested_at"]))
            + f"<small>{escape(status)} ／ {escape(label)}</small></a>"
        )
    content += "</nav><article>"
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
                        + " ／ "
                        + escape(ACTIVITIES[assessment["activity"]])
                        + "</strong><br>"
                        + escape(assessment["reason"])
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
            content += '</div></fieldset><label>動作<select name="activity">'
            for key, activity in ACTIVITIES.items():
                selected_attr = "selected" if key == draft.get("activity", row["activity"]) else ""
                content += f'<option value="{key}" {selected_attr}>{activity}</option>'
            content += (
                '</select></label><p class="note">動画の再生バーで位置を確認し、排泄の開始・完了を秒数で入力します。'
                '分からなければ空欄にしてください。「排泄なし」「判断できない」では時刻を保存しません。</p><div class="times">'
            )
            for field, text in [("start", "開始（動画内の秒）"), ("end", "完了（動画内の秒）")]:
                value = draft.get(field + "_seconds", row["actual_" + field + "_offset"])
                value = "" if value is None else str(value)
                content += (
                    f'<label>{text}<input type="number" min="0" max="{row["duration"]}" '
                    f'step="0.01" name="{field}" value="{value}"></label>'
                )
            content += (
                '</div><p><label>メモ<br><textarea name="note" rows="3" maxlength="2000" style="width:100%">'
                + escape(row["note"])
                + "</textarea></label></p>"
                + "<button>動画ラベルを保存</button></form>"
            )
            if row["event_id"]:
                content += f'<p><a href="/?filter=all&amp;id={row["event_id"]}">対応するイベント画像を確認</a></p>'
        elif row["status"] == "failed":
            content += "<p>" + escape(row["error"] or "動画を保存できませんでした") + "</p>"
        else:
            content += "<p>保存処理中です。録画と変換が終わったら更新してください。</p>"
    return document(content + "</article></div>")


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
            )
        handler.send_response(303)
        handler.send_header("Location", f"/clips?id={clip_id}&saved=1")
        handler.end_headers()
    except (ValueError, TypeError):
        from .label_gui import document

        status = 409 if handler.path == "/clips/request" else 400
        handler.respond(
            status,
            document(
                '<h2>保存できませんでした</h2><p>監視とカメラの接続、保存処理数、動画内の時刻を確認してください。</p><a href="/clips">動画一覧へ戻る</a>'
            ),
        )
