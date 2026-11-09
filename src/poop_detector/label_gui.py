"""画像とラベルを確認する、外部依存のないローカルGUI。"""

import hmac
import json
import secrets
import webbrowser
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .clip_gui import handle_clip_post, location_field, render_clips, stream_video
from .clips import CLIP_LABELS, ClipStore
from .events import EXCRETION_LOCATIONS, EXCRETION_TYPES, Store
from .live_preview import LivePreview

JST = timezone(timedelta(hours=9))
ASSETS = Path(__file__).with_name("static")
LIVE_PANEL = """
<section class="live-panel" aria-labelledby="live-title">
  <h2 id="live-title">現在のカメラ映像</h2>
  <div class="live-grid">
    <figure class="live-view">
      <img id="live-frame" alt="現在のカメラ映像" hidden>
      <figcaption id="live-frame-status">監視からの映像を待っています。</figcaption>
    </figure>
    <div class="live-result">
      <h3>直近のCodex判定</h3>
      <p class="note">保存済みクリップの判定です。ライブ映像と時刻が異なります。</p>
      <p><strong id="live-judgment">判定を読み込み中</strong></p>
      <p id="live-reason"></p>
      <p class="note" id="live-judgment-time"></p>
      <a id="live-clip-link" href="/clips" hidden>判定されたクリップを開く</a>
    </div>
  </div>
</section>
<script src="/assets/live.js" defer></script>
"""
STYLE = """
:root{font-family:"Hiragino Kaku Gothic ProN","Yu Gothic",sans-serif;color:#243b47;background:#edf3f5}
*{box-sizing:border-box}body{margin:0}header{padding:24px 4vw;background:#254b60;color:white;display:flex;justify-content:space-between;align-items:center;gap:16px}
h1{font-size:23px;margin:0 0 6px}p{line-height:1.7;margin:6px 0}header p{font-size:13px;color:#d6e8ee}
main{max-width:1600px;margin:auto;padding:24px 4vw}.toolbar{display:flex;align-items:center;gap:12px;margin-bottom:18px;flex-wrap:wrap}
.workspace{display:grid;grid-template-columns:260px minmax(0,1fr);gap:24px}nav{max-height:78vh;overflow:auto}nav a{display:block;padding:14px;text-decoration:none;color:inherit;border-bottom:1px solid #c8d8df;background:#fff}nav a.active{border-left:5px solid #247c90;background:#dceef3}small{display:block;font-size:12px;margin-top:5px;color:#526d7a}
article{background:white;padding:22px;border-radius:12px}figure{margin:0 0 18px;background:#192d39;min-height:120px;text-align:center;border-radius:8px;overflow:hidden}figure img{max-width:100%;max-height:62vh;display:block;margin:auto}figcaption{padding:10px;color:#dae8ee;font-size:13px}
fieldset{border:0;padding:0;margin:18px 0}legend{font-weight:bold;margin-bottom:12px}.choices{display:flex;gap:12px;flex-wrap:wrap}.choice{border:2px solid #d2e0e6;border-radius:8px;padding:14px;cursor:pointer}.choice:has(input:checked){border-color:#247c90;background:#e7f3f6}input[type=radio]{accent-color:#247c90;margin-right:6px}
.times{display:flex;gap:20px;flex-wrap:wrap}label{font-size:14px}input[type=datetime-local],select{display:block;padding:10px;border:1px solid #93adb9;border-radius:5px;font:inherit;margin-top:7px}button{font:inherit;padding:11px 18px;border:1px solid #247c90;background:#247c90;color:white;border-radius:6px;cursor:pointer}button.secondary{background:white;color:#254b60}button:hover{filter:brightness(.94)}a{color:#19677c}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #df9d19;outline-offset:3px}.note{font-size:13px;color:#526d7a}.message{background:#e2f2e8;padding:12px;margin-bottom:16px;border-left:4px solid #38835c}details{margin-top:24px;padding:20px;background:white;border-radius:8px}summary{cursor:pointer;font-weight:bold}details form{margin-top:18px}.empty{padding:50px 24px;text-align:center}footer{margin-top:20px;color:#526d7a;font-size:12px}
.live-panel{margin-bottom:24px}.live-panel h2{margin:0 0 12px}.live-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(220px,1fr);gap:16px;align-items:stretch}
.live-view{margin:0}.live-view img{width:100%;aspect-ratio:16/9;object-fit:contain}.live-view img[hidden]{display:none}.live-view figcaption{padding:10px 12px;text-align:left}.live-result{background:white;border-radius:12px;padding:16px 20px}.live-result h3{margin:0 0 8px}
@media(max-width:800px){header{display:block}.workspace,.live-grid{grid-template-columns:1fr}nav{max-height:190px}article{padding:16px}.times{gap:12px}main{padding:16px}}
"""


def local_time(value, form=False):
    if not value:
        return ""
    stamp = datetime.fromisoformat(value).astimezone(JST)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S" if form else "%Y/%m/%d %H:%M:%S")


def parse_time(value):
    if not value:
        return None
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=JST)
    return stamp.isoformat()


def document(content):
    return (
        '<!doctype html><html lang="ja"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>ここちゃんのトイレ記録</title><style>" + STYLE + "</style>"
        "<header><div><h1>ここちゃんのトイレ記録</h1><p>画像を確認して、検知結果にラベルを付けます。</p>"
        "</div><span>このMac内で保存</span></header><main>" + content + "</main></html>"
    )


def render(store, token, query):
    filters = {
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
    mode = query.get("filter", ["unlabeled"])[0]
    if mode not in filters:
        mode = "unlabeled"
    rows = list(reversed(store.list()))
    rows = [
        r
        for r in rows
        if mode == "all"
        or (mode == "missed" and r["kind"] == "MISSED")
        or (mode == "manual" and r["kind"] in ("MANUAL", "VIDEO"))
        or (mode == "true" and r["feedback"] == 1)
        or (
            mode in ("feces", "urine", "both")
            and r["feedback"] == 1
            and (r["excretion_type"] == mode or (mode != "both" and r["excretion_type"] == "both"))
        )
        or (
            r["kind"] == "SUSPECTED"
            and (
                (mode == "unlabeled" and r["feedback"] is None)
                or (mode == "false" and r["feedback"] == 0)
            )
        )
    ]
    selected = query.get("id", [""])[0]
    row = next((r for r in rows if r["id"] == selected), rows[0] if rows else None)
    metrics = store.metrics()
    content = '<div class="toolbar"><form method="get"><label>表示する記録<select name="filter">'
    content += "".join(
        f'<option value="{k}" {"selected" if k == mode else ""}>{v}</option>'
        for k, v in filters.items()
    )
    content += '</select></label> <button class="secondary">表示</button></form>'
    counts = metrics["excretion_counts"]
    content += (
        f"<p>未ラベル {metrics['unlabeled']}件 ／ 排便 {counts['feces']}件"
        f" ／ 排尿 {counts['urine']}件 ／ 種類不明 {counts['unknown']}件</p>"
        '<a href="/records">排便・排尿の記録を見る</a> <a href="/clips">動画を確認・保存する</a></div>'
    )
    if query.get("saved"):
        content += '<div class="message" role="status">保存しました。</div>'
    content += (
        '<p class="note">自動検知は排泄の疑いです。便・尿の種類は確認して記録してください。'
        "尿跡が残っていても追加の変化を監視します。同じ跡は再通知せず、判断できない排泄は手動で記録できます。</p>"
    )
    content += '<div class="workspace"><nav aria-label="イベント一覧">'
    for item in rows:
        label = (
            EXCRETION_TYPES[item["excretion_type"]]
            if item["feedback"] == 1
            else "誤検知"
            if item["feedback"] == 0
            else "未ラベル"
        )
        label += {"MISSED": "（見逃し）", "MANUAL": "（手動）", "VIDEO": "（映像）"}.get(
            item["kind"], ""
        )
        url = "/?" + urlencode({"filter": mode, "id": item["id"]})
        active = "active" if row and row["id"] == item["id"] else ""
        content += (
            f'<a class="{active}" href="{escape(url)}">'
            f"{escape(local_time(item['timestamp']))}<small>{label}</small></a>"
        )
    content += "</nav><article>"
    csrf = f'<input type="hidden" name="csrf" value="{token}">'
    if row:
        content += f"<h2>{escape(local_time(row['timestamp']))}</h2>"
        if row["image"]:
            content += (
                f'<figure><img src="/image?id={escape(row["id"])}" alt="検知時のカメラ画像">'
                "<figcaption>検知時の画像。静止画だけで判断できない場合は保留してください。</figcaption></figure>"
            )
        else:
            content += "<p>この記録には画像がありません。</p>"
        if row["kind"] in ("SUSPECTED", "MANUAL", "MISSED", "VIDEO"):
            content += (
                '<form method="post" action="/save">'
                + csrf
                + f'<input type="hidden" name="id" value="{escape(row["id"])}">'
                + f'<input type="hidden" name="filter" value="{mode}">'
                + '<fieldset><legend>実際にはどうでしたか？</legend><div class="choices">'
            )
            choices = list(EXCRETION_TYPES.items())
            if row["kind"] == "SUSPECTED":
                choices += [("false", "誤検知"), ("unknown", "保留・未ラベル")]
            elif row["kind"] == "VIDEO":
                choices += [("unknown", "保留・未ラベル")]
            selected_label = (
                row["excretion_type"]
                if row["feedback"] == 1
                else "false"
                if row["feedback"] == 0
                else "unknown"
            )
            for value, label in choices:
                if value == "feces":
                    label = "排便だった"
                checked = "checked" if value == selected_label else ""
                content += (
                    f'<label class="choice"><input type="radio" name="label" '
                    f'value="{value}" {checked} required>{label}</label>'
                )
            content += "</div></fieldset>" + location_field(row["location"])
            content += '<p class="note">「種類不明」は排泄を確認したが便・尿の区別が付かない場合に選びます。開始・完了時刻は分かる場合だけ入力してください。日本時間です。</p><div class="times">'
            for name, label in [("start", "排泄の開始"), ("end", "排泄の完了")]:
                value = escape(local_time(row["actual_" + name], form=True))
                content += f'<label>{label}<input type="datetime-local" step="1" name="{name}" value="{value}"></label>'
            content += (
                '</div><p><label>メモ（任意）<br><textarea name="note" rows="3" maxlength="2000" style="width:100%">'
                + escape(row["note"])
                + "</textarea></label></p>"
                + '<p style="margin-top:20px"><button>ラベルを保存</button></p></form>'
            )
        else:
            content += "<p>通知されなかった排便として登録されています。</p>"
    else:
        content += '<div class="empty"><h2>該当する記録はありません</h2><p>「すべて」に切り替えるか、監視で記録が増えたら再表示してください。</p></div>'
    content += "</article></div><details><summary>目視で確認した排便・排尿を記録する</summary>"
    content += (
        '<p class="note">既に通知の画像がある場合は、その記録にラベルを付けてください。同じ排泄の重複登録を避けてください。</p>'
        '<form method="post" action="/save">'
        + csrf
        + '<label>記録の種類<select name="action"><option value="manual">通常の手動記録</option><option value="missed">通知されなかった排泄（見逃し）</option></select></label>'
        + '<label>排泄の種類<select name="type">'
        + "".join(f'<option value="{k}">{v}</option>' for k, v in EXCRETION_TYPES.items())
        + "</select></label>"
        + location_field(None)
        + '<div class="times">'
        '<label>開始（日本時間・必須）<input type="datetime-local" step="1" name="start" required></label>'
        '<label>完了（任意）<input type="datetime-local" step="1" name="end"></label>'
        '</div><p><label>メモ<br><textarea name="note" rows="2" maxlength="2000" style="width:100%"></textarea></label></p><p style="margin-top:16px"><button>排泄を記録</button></p></form></details>'
        "<footer>ラベルは画像と同じ保存先のSQLiteに記録します。登録だけで自動学習は行いません。</footer>"
    )
    return document(LIVE_PANEL + content)


def render_records(store, clips):
    video_links = clips.record_videos()
    content = '<h2>排便・排尿の記録</h2><p><a href="/">画像の確認・記録の追加へ戻る</a></p>'
    content += '<p class="note">確認済みの排泄を表示します。両方の記録は排便・排尿に各1件と数えます。時刻は日本時間です。</p><div style="overflow:auto"><table style="width:100%;background:white;border-collapse:collapse"><thead><tr><th>開始・記録時刻</th><th>完了</th><th>種類</th><th>場所</th><th>登録方法</th><th>メモ</th><th>映像</th><th>操作</th></tr></thead><tbody>'
    for row in reversed(store.records()):
        url = "/?" + urlencode({"filter": "all", "id": row["id"]})
        source = {
            "MANUAL": "手動",
            "MISSED": "見逃し",
            "SUSPECTED": "検知を確認",
            "VIDEO": "映像で確認",
        }[row["kind"]]
        recorded_at = local_time(row["actual_start"] or row["timestamp"])
        if row["kind"] == "VIDEO" and not row["actual_start"]:
            recorded_at += "（動画時刻・開始不明）"
        values = [
            recorded_at,
            local_time(row["actual_end"]),
            EXCRETION_TYPES[row["excretion_type"]],
            EXCRETION_LOCATIONS.get(row["location"], "場所不明"),
            source,
            row["note"],
        ]
        content += "<tr>" + "".join(
            '<td style="padding:14px;border-bottom:1px solid #c8d8df">' + escape(v) + "</td>"
            for v in values
        )
        links = " ".join(
            f'<a href="/clips?id={escape(clip_id, quote=True)}">映像を見る{f" ({index})" if index > 1 else ""}</a>'
            for index, clip_id in enumerate(video_links.get(row["id"], ()), 1)
        )
        content += f'<td>{links}</td><td><a href="{escape(url)}">編集</a></td></tr>'
    content += "</tbody></table></div>"
    if not store.records():
        content += "<p>確認済みの排泄記録はまだありません。</p>"
    return document(content)


def make_server(root, port=8765):
    store = Store(root)
    clips = ClipStore(root)
    preview = LivePreview(root)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, data, mime="text/html; charset=utf-8", headers=None):
            data = data.encode() if isinstance(data, str) else data
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; img-src 'self' blob:; media-src 'self'; "
                "script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; "
                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def host_allowed(self):
            port = self.server.server_port
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def do_GET(self):
            if not self.host_allowed():
                return self.respond(403, "アクセスできません")
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                return self.respond(200, render(store, token, query))
            if url.path in ("/assets/live.js", "/assets/clips.js"):
                script = ASSETS / url.path.rsplit("/", 1)[1]
                return self.respond(200, script.read_bytes(), "text/javascript; charset=utf-8")
            if url.path == "/live/frame":
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    return self.respond(403, "アクセスできません")
                frame = preview.read()
                if frame is None:
                    return self.respond(503, "現在の映像を取得できません")
                data, stamp = frame
                headers = {"X-Frame-Time": stamp}
                tray_state = preview.tray_state()
                if tray_state in ("tracking", "searching"):
                    headers["X-Tray-State"] = tray_state
                return self.respond(200, data, "image/jpeg", headers)
            if url.path == "/live/assessment":
                row = clips.latest_assessment()
                data = None
                if row:
                    data = {
                        "clip_id": row["clip_id"],
                        "label": "判定失敗" if row["error"] else CLIP_LABELS[row["label"]],
                        "reason": row["error"] or row["reason"],
                        "requested_at": row["requested_at"],
                        "assessed_at": row["assessed_at"],
                        "human_label": CLIP_LABELS.get(row["human_label"]),
                        "location": EXCRETION_LOCATIONS.get(row["location"]),
                        "human_location": EXCRETION_LOCATIONS.get(row["human_location"]),
                    }
                return self.respond(200, json.dumps(data), "application/json; charset=utf-8")
            if url.path == "/records":
                return self.respond(200, render_records(store, clips))
            if url.path == "/clips":
                return self.respond(200, render_clips(root, token, query))
            if url.path == "/clip-video":
                return stream_video(self, root, query.get("id", [""])[0])
            if url.path == "/image":
                event = next((r for r in store.list() if r["id"] == query.get("id", [""])[0]), None)
                if event and event["image"]:
                    path = (store.root / event["image"]).resolve()
                    if path.is_relative_to((store.root / "events").resolve()) and path.is_file():
                        return self.respond(200, path.read_bytes(), "image/jpeg")
            return self.respond(404, "画像または記録が見つかりません")

        def do_POST(self):
            if not self.host_allowed():
                return self.respond(403, "アクセスできません")
            if self.path not in ("/save", "/clips/request", "/clips/label", "/clips/confirm"):
                return self.respond(404, "記録が見つかりません")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError
                data = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
                fields = {key: values[0] for key, values in data.items()}
                if not hmac.compare_digest(fields.get("csrf", ""), token):
                    return self.respond(403, "画面を再読み込みして保存してください")
                if self.path.startswith("/clips/"):
                    return handle_clip_post(self, root, fields)
                event_id = fields.get("id")
                if fields.get("action") in ("missed", "manual"):
                    start = parse_time(fields.get("start"))
                    end = parse_time(fields.get("end"))
                    if not start:
                        raise ValueError
                else:
                    row = next(
                        (r for r in store.list() if r["id"] == event_id),
                        None,
                    )
                    if row is None or fields.get("label") not in (
                        *EXCRETION_TYPES,
                        "true",
                        "false",
                        "unknown",
                    ):
                        raise ValueError
                    start = (
                        parse_time(fields["start"]) if "start" in fields else row["actual_start"]
                    )
                    end = parse_time(fields["end"]) if "end" in fields else row["actual_end"]
                if start and end and datetime.fromisoformat(end) < datetime.fromisoformat(start):
                    raise ValueError
                if fields.get("action") in ("missed", "manual"):
                    store.add_excretion(
                        fields.get("type", "unspecified"),
                        start,
                        end,
                        fields.get("note", ""),
                        missed=fields["action"] == "missed",
                        location=fields.get("location"),
                    )
                else:
                    label = "unspecified" if fields["label"] == "true" else fields["label"]
                    store.classify(
                        event_id,
                        label,
                        start,
                        end,
                        fields.get("note", row["note"]),
                        location=fields.get("location"),
                    )
                mode = fields.get("filter", "unlabeled")
                if mode not in (
                    "all",
                    "unlabeled",
                    "true",
                    "false",
                    "missed",
                    "manual",
                    "feces",
                    "urine",
                    "both",
                ):
                    mode = "unlabeled"
                self.send_response(303)
                self.send_header("Location", "/?" + urlencode({"filter": mode, "saved": "1"}))
                self.end_headers()
            except (ValueError, UnicodeError, TypeError):
                self.respond(
                    400,
                    document(
                        '<h2>保存できませんでした</h2><p>ラベルと時刻を確認してください。完了は開始以降を指定します。</p><a href="/">記録に戻る</a>'
                    ),
                )

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(root, port=8765, open_browser=True):
    with make_server(root, port) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"ラベル付け画面: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("ラベル付け画面を終了しました")
