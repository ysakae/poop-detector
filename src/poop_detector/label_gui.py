"""画像とラベルを確認する、外部依存のないローカルGUI。"""

import hmac
import secrets
import webbrowser
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from .events import Store

JST = timezone(timedelta(hours=9))
STYLE = """
:root{font-family:"Hiragino Kaku Gothic ProN","Yu Gothic",sans-serif;color:#243b47;background:#edf3f5}
*{box-sizing:border-box}body{margin:0}header{padding:24px 4vw;background:#254b60;color:white;display:flex;justify-content:space-between;align-items:center;gap:16px}
h1{font-size:23px;margin:0 0 6px}p{line-height:1.7;margin:6px 0}header p{font-size:13px;color:#d6e8ee}
main{max-width:1600px;margin:auto;padding:24px 4vw}.toolbar{display:flex;align-items:center;gap:12px;margin-bottom:18px;flex-wrap:wrap}
.workspace{display:grid;grid-template-columns:260px minmax(0,1fr);gap:24px}nav{max-height:78vh;overflow:auto}nav a{display:block;padding:14px;text-decoration:none;color:inherit;border-bottom:1px solid #c8d8df;background:#fff}nav a.active{border-left:5px solid #247c90;background:#dceef3}small{display:block;font-size:12px;margin-top:5px;color:#526d7a}
article{background:white;padding:22px;border-radius:12px}figure{margin:0 0 18px;background:#192d39;min-height:120px;text-align:center;border-radius:8px;overflow:hidden}figure img{max-width:100%;max-height:62vh;display:block;margin:auto}figcaption{padding:10px;color:#dae8ee;font-size:13px}
fieldset{border:0;padding:0;margin:18px 0}legend{font-weight:bold;margin-bottom:12px}.choices{display:flex;gap:12px;flex-wrap:wrap}.choice{border:2px solid #d2e0e6;border-radius:8px;padding:14px;cursor:pointer}.choice:has(input:checked){border-color:#247c90;background:#e7f3f6}input[type=radio]{accent-color:#247c90;margin-right:6px}
.times{display:flex;gap:20px;flex-wrap:wrap}label{font-size:14px}input[type=datetime-local],select{display:block;padding:10px;border:1px solid #93adb9;border-radius:5px;font:inherit;margin-top:7px}button{font:inherit;padding:11px 18px;border:1px solid #247c90;background:#247c90;color:white;border-radius:6px;cursor:pointer}button.secondary{background:white;color:#254b60}button:hover{filter:brightness(.94)}a{color:#19677c}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #df9d19;outline-offset:3px}.note{font-size:13px;color:#526d7a}.message{background:#e2f2e8;padding:12px;margin-bottom:16px;border-left:4px solid #38835c}details{margin-top:24px;padding:20px;background:white;border-radius:8px}summary{cursor:pointer;font-weight:bold}details form{margin-top:18px}.empty{padding:50px 24px;text-align:center}footer{margin-top:20px;color:#526d7a;font-size:12px}
@media(max-width:800px){header{display:block}.workspace{grid-template-columns:1fr}nav{max-height:190px}article{padding:16px}.times{gap:12px}main{padding:16px}}
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
        "true": "排便だった",
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
        or (
            r["kind"] == "SUSPECTED"
            and (
                (mode == "unlabeled" and r["feedback"] is None)
                or (mode == "true" and r["feedback"] == 1)
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
    content += (
        f"<p>未ラベル {metrics['unlabeled']}件 ／ 排便 {metrics['true_positive']}件"
        f" ／ 誤検知 {metrics['false_positive']}件 ／ 見逃し {metrics['missed']}件</p></div>"
    )
    if query.get("saved"):
        content += '<div class="message" role="status">保存しました。</div>'
    content += '<div class="workspace"><nav aria-label="イベント一覧">'
    for item in rows:
        label = (
            "見逃し"
            if item["kind"] == "MISSED"
            else {None: "未ラベル", 0: "誤検知", 1: "排便だった"}[item["feedback"]]
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
        if row["kind"] == "SUSPECTED":
            content += (
                '<form method="post" action="/save">'
                + csrf
                + f'<input type="hidden" name="id" value="{escape(row["id"])}">'
                + f'<input type="hidden" name="filter" value="{mode}">'
                + '<fieldset><legend>実際にはどうでしたか？</legend><div class="choices">'
            )
            for value, label, match in [
                ("true", "排便だった", 1),
                ("false", "誤検知", 0),
                ("unknown", "保留・未ラベル", None),
            ]:
                checked = "checked" if row["feedback"] == match else ""
                content += (
                    f'<label class="choice"><input type="radio" name="label" '
                    f'value="{value}" {checked} required>{label}</label>'
                )
            content += '</div></fieldset><p class="note">開始・完了時刻は分かる場合だけ入力してください。日本時間です。</p><div class="times">'
            for name, label in [("start", "排便の開始"), ("end", "排便の完了")]:
                value = escape(local_time(row["actual_" + name], form=True))
                content += f'<label>{label}<input type="datetime-local" step="1" name="{name}" value="{value}"></label>'
            content += '</div><p style="margin-top:20px"><button>ラベルを保存</button></p></form>'
        else:
            content += "<p>通知されなかった排便として登録されています。</p>"
    else:
        content += '<div class="empty"><h2>該当する記録はありません</h2><p>「すべて」に切り替えるか、監視で記録が増えたら再表示してください。</p></div>'
    content += "</article></div><details><summary>通知されなかった排便を登録する</summary>"
    content += (
        '<p class="note">実際に確認した排便だけを登録してください。同じ排便を重複登録しないでください。</p>'
        '<form method="post" action="/save">'
        + csrf
        + '<input type="hidden" name="action" value="missed"><div class="times">'
        '<label>開始（日本時間・必須）<input type="datetime-local" step="1" name="start" required></label>'
        '<label>完了（任意）<input type="datetime-local" step="1" name="end"></label>'
        '</div><p style="margin-top:16px"><button>見逃しを登録</button></p></form></details>'
        "<footer>ラベルは画像と同じ保存先のSQLiteに記録します。登録だけで自動学習は行いません。</footer>"
    )
    return document(content)


def make_server(root, port=8765):
    store = Store(root)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, data, mime="text/html; charset=utf-8"):
            data = data.encode() if isinstance(data, str) else data
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
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
            if self.path != "/save":
                return self.respond(404, "記録が見つかりません")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError
                data = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
                fields = {key: values[0] for key, values in data.items()}
                if not hmac.compare_digest(fields.get("csrf", ""), token):
                    return self.respond(403, "画面を再読み込みして保存してください")
                event_id = fields.get("id")
                if fields.get("action") == "missed":
                    start = parse_time(fields.get("start"))
                    end = parse_time(fields.get("end"))
                    if not start:
                        raise ValueError
                else:
                    row = next(
                        (
                            r
                            for r in store.list()
                            if r["id"] == event_id and r["kind"] == "SUSPECTED"
                        ),
                        None,
                    )
                    if row is None or fields.get("label") not in ("true", "false", "unknown"):
                        raise ValueError
                    start = (
                        parse_time(fields["start"]) if "start" in fields else row["actual_start"]
                    )
                    end = parse_time(fields["end"]) if "end" in fields else row["actual_end"]
                if start and end and datetime.fromisoformat(end) < datetime.fromisoformat(start):
                    raise ValueError
                if fields.get("action") == "missed":
                    store.missed(start, end)
                else:
                    store.update(
                        event_id,
                        feedback={"true": 1, "false": 0, "unknown": None}[fields["label"]],
                        actual_start=start,
                        actual_end=end,
                    )
                mode = fields.get("filter", "unlabeled")
                if mode not in ("all", "unlabeled", "true", "false", "missed"):
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
