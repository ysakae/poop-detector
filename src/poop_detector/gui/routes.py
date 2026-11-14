"""GUI の URL ごとの処理。

ルートを足すときは、関数を1つ書いて @get または @post を付ける。管理者だけの操作は
role="admin" を付けて登録する（既定はログインした人なら誰でも使える "member"）。
安全対策（Host 検査、セッション、POST の CSRF、役割）は asgi.py がすべてのルートの前に行う。
ルートは要求ごとの Exchange を受け取り、respond・redirect・stream で応答を決める。
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias
from urllib.parse import parse_qs, urlencode, urlsplit

from ..detection.roi import ROI, outline
from ..features.activity_map import ActivityLayout
from ..features.tray_registration import REGISTRATION_MESSAGES, TrayRegistration
from ..records.events import EXCRETION_LOCATIONS, EXCRETION_TYPES, EventRow
from ..video.clips import CLIP_LABELS
from .access import expired_cookie, session_ids
from .accounts import ROLES, Role
from .activity_page import render_activity
from .auth_api import safe_next
from .clip_pages import (
    capacity_status,
    handle_benchmark_post,
    handle_clip_post,
    render_clips,
    render_storage,
    stream_video,
)
from .pages import (
    EVENTS_PAGE,
    Query,
    local_time,
    message_page,
    outage_rows,
    parse_time,
    render_events,
    render_home,
    render_records,
    save_failed_page,
)
from .passkeys import PasskeyError
from .review_queue import NEXT_PAGE, review_bar, review_items
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

ASSETS = Path(__file__).with_name("static")
# 管理の画面を /manage の下へまとめる前の URL（#86）。古いブックマークを新しい場所へ移す。
MOVED_PAGES = {
    "/clips/storage": "/manage/storage",
    "/account/users": "/manage/users",
    "/account/invite": "/manage/invite",
    "/account/audit": "/manage/audit",
}
FILTERS = ("all", "unlabeled", "true", "false", "missed", "manual", "feces", "urine", "both")


@dataclass(frozen=True)
class Form:
    """POST のフォーム。fields は各名前の最初の値、values は同じ名前のすべての値。"""

    fields: Mapping[str, str]
    values: Mapping[str, list[str]]


GetRoute: TypeAlias = Callable[["Handler", Query], None]
PostRoute: TypeAlias = Callable[["Handler", Form], None]
GET_ROUTES: dict[str, GetRoute] = {}
POST_ROUTES: dict[str, PostRoute] = {}
# (メソッド, パス) ごとに、使うのに必要な役割。表にないルートは "member"。
ROUTE_ROLES: dict[tuple[str, str], Role] = {}
ADMIN_ONLY = "この操作は管理者だけができます"


def required_role(method: str, path: str) -> Role:
    return ROUTE_ROLES.get((method, path), "member")


def get(*paths: str, role: Role = "member") -> Callable[[GetRoute], GetRoute]:
    """GET のルートとして登録する。role の役割を持たない人には asgi.py が 403 を返す。"""

    def register(route: GetRoute) -> GetRoute:
        for path in paths:
            GET_ROUTES[path] = route
            ROUTE_ROLES[("GET", path)] = role
        return route

    return register


def post(*paths: str, role: Role = "member") -> Callable[[PostRoute], PostRoute]:
    """POST のルートとして登録する。CSRF トークンと役割は確認済みで呼ばれる。"""

    def register(route: PostRoute) -> PostRoute:
        for path in paths:
            POST_ROUTES[path] = route
            ROUTE_ROLES[("POST", path)] = role
        return route

    return register


def admin_only_page(title: str | None) -> str:
    """管理者でない人が管理者だけの操作を開いたときの画面。"""
    return message_page(
        "管理者だけの操作です",
        f"{ADMIN_ONLY}。必要なときは、管理者に依頼してください。",
        "/",
        "トップへ戻る",
        title=title,
        wrap_link=True,
    )


def is_admin(handler: "Handler") -> bool:
    return handler.user is not None and handler.user.role == "admin"


def audit(handler: "Handler", action: str, detail: str = "") -> None:
    """ログインしている人の操作として監査ログに残す。"""
    if handler.user is None:
        raise RuntimeError("ログインしていない要求の操作を監査ログに残そうとしました")
    handler.app.accounts.audit.record(handler.user.id, action, detail)


def first(query: Query, name: str, default: str = "") -> str:
    return query.get(name, [default])[0]


# --- 画面 ---


def _review_bar(handler: "Handler", query: Query, key: str) -> dict[str, Any] | None:
    """確認待ちから開いた画面（from=review）なら、残りの件数と「飛ばす」の帯の値。"""
    if first(query, "from") != "review":
        return None
    return review_bar(review_items(handler.app.store, handler.app.clips), key)


@get("/")
def home_page(handler: "Handler", _query: Query) -> None:
    """ホーム（ダッシュボード）。問い合わせ文字列付きの古いリンクは検知の一覧へ移す。"""
    query = urlsplit(handler.path).query
    if query:
        # 検知の一覧がホームにあった頃の URL（/?filter=…&id=…）。
        handler.redirect(f"{EVENTS_PAGE}?{query}", status=301)
        return
    app = handler.app
    pending = len(review_items(app.store, app.clips))
    handler.respond(
        200,
        render_home(
            app.store, app.disk_mb, title=app.title, pending=pending, manual_only=app.manual_only
        ),
    )


@get(EVENTS_PAGE)
def events_page(handler: "Handler", query: Query) -> None:
    app = handler.app
    page = render_events(
        app.store,
        handler.csrf,
        query,
        title=app.title,
        review=_review_bar(handler, query, f"event:{first(query, 'id')}"),
    )
    handler.respond(200, page)


@get("/records")
def records_page(handler: "Handler", query: Query) -> None:
    app = handler.app
    handler.respond(200, render_records(app.store, app.clips, query, title=app.title))


@get("/activity")
def activity_page(handler: "Handler", query: Query) -> None:
    handler.respond(200, render_activity(handler.app.root, query, title=handler.app.title))


@get("/clips")
def clips_page(handler: "Handler", query: Query) -> None:
    app = handler.app
    review = _review_bar(handler, query, f"clip:{first(query, 'id')}")
    page = render_clips(app.root, handler.csrf, query, app.disk_mb, title=app.title, review=review)
    handler.respond(200, page)


@get("/manage/storage", role="admin")
def storage_page(handler: "Handler", query: Query) -> None:
    app = handler.app
    page = render_storage(app.clips, handler.csrf, query, app.disk_mb, title=app.title)
    handler.respond(200, page)


@get(*MOVED_PAGES)
def moved_page(handler: "Handler", _query: Query) -> None:
    """移した画面へ 301 で移す。役割は移った先のルートが確かめる。"""
    url = urlsplit(handler.path)
    target = MOVED_PAGES[url.path]
    handler.redirect(f"{target}?{url.query}" if url.query else target, status=301)


@get("/clips/capacity")
def capacity(handler: "Handler", _query: Query) -> None:
    handler.respond(200, capacity_status(handler.app.clips.root, handler.app.disk_mb))


def management_status(handler: "Handler") -> dict[str, Any]:
    app = handler.app
    status = TrayRegistration(app.root).status()
    monitor = app.preview.monitor_status()
    snapshot = app.preview.read()
    return {
        "phase": status["phase"],
        "message": status["error"] or REGISTRATION_MESSAGES[status["phase"]],
        "monitor": "停止中または未確認" if monitor == "offline" else "稼働中",
        "camera": {"connected": "接続中", "searching": "再接続中"}.get(monitor, "未確認"),
        "frame_time": local_time(snapshot[1]) if snapshot else "未取得",
        "tracking": (
            {"tracking": "追従中", "searching": "探索中", "fixed": "固定範囲"}.get(
                app.preview.tray_state(), "未確認"
            )
            if monitor != "offline"
            else "未確認"
        ),
    }


def render_management(handler: "Handler", error: str | None = None) -> str:
    app = handler.app
    try:
        roi = ROI.load(TrayRegistration(app.root).scene_root() / "roi.json")
        points = (outline(roi, (1001, 1001, 3)) / 1000).tolist()
    except (OSError, ValueError, TypeError):
        points = []
    return render_template(
        "management.html",
        title=app.title,
        token=handler.csrf,
        points=points,
        error=error,
        outages=outage_rows(app.outages.recent()),
        **management_status(handler),
    )


@get("/manage", role="admin")
def management_page(handler: "Handler", _query: Query) -> None:
    handler.respond(200, render_management(handler))


@get("/manage/status", role="admin")
def management_status_json(handler: "Handler", _query: Query) -> None:
    handler.respond(200, json.dumps(management_status(handler)), "application/json; charset=utf-8")


@post("/manage/tray", role="admin")
def management_tray(handler: "Handler", form: Form) -> None:
    registration = TrayRegistration(handler.app.root)
    try:
        action = form.fields.get("action")
        if action == "begin":
            registration.begin()
        elif action == "cancel":
            registration.cancel()
        elif action == "apply":
            if form.fields.get("confirm_empty") != "yes":
                raise ValueError("空のトレーを確認してください")
            value = form.fields.get("roi")
            roi = ROI.from_mapping(json.loads(value)) if value else None
            registration.apply(roi)
        else:
            raise ValueError("操作を確認してください")
    except (ValueError, TypeError, AttributeError, OSError):
        handler.respond(
            400,
            render_management(handler, "操作できません。トレーの状態と範囲を確認してください。"),
        )
        return
    audit(handler, f"tray.{action}")
    handler.redirect("/manage")


# --- ログインとパスキーの登録（ログインしていなくても開ける） ---


def localhost_redirect(handler: "Handler") -> bool:
    """IP アドレスで開いたときは、パスキーを使える localhost の同じ画面へ移す。"""
    port = handler.app.site.port
    if (handler.headers.get("host") or "").lower() != f"127.0.0.1:{port}":
        return False
    handler.redirect(f"http://localhost:{port}{handler.path}")
    return True


@get("/login")
def login_page(handler: "Handler", query: Query) -> None:
    if localhost_redirect(handler):
        return
    page = render_template(
        "login.html", title=handler.app.title, next_path=safe_next(first(query, "next", "/"))
    )
    handler.respond(200, page)


@get("/enroll")
def enroll_page(handler: "Handler", query: Query) -> None:
    if localhost_redirect(handler):
        return
    app = handler.app
    code = first(query, "code")
    try:
        enrollment = app.passkeys.enrollment(code)
    except PasskeyError as error:
        page = message_page(
            "登録用 URL を使えません", str(error), "/login", "ログインへ", title=app.title
        )
        handler.respond(400, page)
        return
    page = render_template(
        "enroll.html",
        title=app.title,
        person=enrollment.name or "ユーザー",
        role=ROLES[enrollment.role],
        adding=enrollment.user_id is not None,
        code=code,
        expires=local_time(enrollment.expires_at),
    )
    handler.respond(200, page)


# --- ログアウト ---


@get("/logout")
def logout_page(handler: "Handler", _query: Query) -> None:
    app = handler.app
    handler.respond(200, render_template("logout.html", title=app.title, token=handler.csrf))


@post("/logout")
def logout(handler: "Handler", _form: Form) -> None:
    """この要求の Cookie のセッションを終え、ブラウザーの Cookie を消す。"""
    for session_id in session_ids(handler.headers.get("cookie")):
        handler.app.sessions.revoke(session_id)
    page = message_page(
        "ログアウトしました",
        "もう一度使うには、ログインの画面からパスキーでログインしてください。",
        "/login",
        "ログインの画面へ",
        title=handler.app.title,
        wrap_link=True,
    )
    secure = handler.scheme == "https"
    handler.respond(200, page, headers={"Set-Cookie": expired_cookie(secure=secure)})


# --- 画像・動画・スクリプト ---


@get(
    "/assets/live.js",
    "/assets/clips.js",
    "/assets/clip_storage.js",
    "/assets/management.js",
    "/assets/areas.js",
    "/assets/push.js",
    "/assets/passkey.js",
)
def asset(handler: "Handler", _query: Query) -> None:
    # 許可したパスだけが登録されているので、ファイル名はこの中のどれか。
    script = ASSETS / urlsplit(handler.path).path.rsplit("/", 1)[1]
    handler.respond(200, script.read_bytes(), "text/javascript; charset=utf-8")


@get("/assets/paw.svg")
def paw_icon(handler: "Handler", _query: Query) -> None:
    """ワンちゃんのテーマの肉球の模様（CSP の img-src 'self' で読めるように同梱する）。"""
    handler.respond(200, (ASSETS / "paw.svg").read_bytes(), "image/svg+xml")


@get("/image")
def event_image(handler: "Handler", query: Query) -> None:
    """イベントの画像。events/ の外を指す記録は返さない。"""
    store = handler.app.store
    event = next((r for r in store.list() if r["id"] == first(query, "id")), None)
    if event and event["image"]:
        path = (store.root / event["image"]).resolve()
        if path.is_relative_to((store.root / "events").resolve()) and path.is_file():
            handler.respond(200, path.read_bytes(), "image/jpeg")
            return
    handler.respond(404, "画像または記録が見つかりません")


@get("/clip-video")
def clip_video(handler: "Handler", query: Query) -> None:
    stream_video(handler, handler.app.root, first(query, "id"))


@get("/activity/background")
def activity_background(handler: "Handler", query: Query) -> None:
    root = handler.app.root
    try:
        layout = ActivityLayout.load(root)
        background = layout.background(root)
        if query.get("id") == [layout.id] and not background.is_symlink():
            handler.respond(200, background.read_bytes(), "image/jpeg")
            return
    except (OSError, ValueError, TypeError, KeyError):
        pass
    handler.respond(404, "背景が見つかりません")


# --- ライブ映像 ---


@get("/live/frame")
def live_frame(handler: "Handler", _query: Query) -> None:
    if handler.headers.get("Sec-Fetch-Site") == "cross-site":
        handler.respond(403, "アクセスできません")
        return
    preview = handler.app.preview
    frame = preview.read()
    if frame is None:
        handler.respond(503, "現在の映像を取得できません")
        return
    data, stamp = frame
    headers = {"X-Frame-Time": stamp}
    tray_state = preview.tray_state()
    if tray_state in ("tracking", "searching"):
        headers["X-Tray-State"] = tray_state
    handler.respond(200, data, "image/jpeg", headers)


@get("/live/status")
def live_status(handler: "Handler", _query: Query) -> None:
    if handler.headers.get("Sec-Fetch-Site") == "cross-site":
        handler.respond(403, "アクセスできません")
        return
    preview = handler.app.preview
    payload = {"frame_time": preview.frame_time(), "tray_state": preview.tray_state()}
    handler.respond(200, json.dumps(payload), "application/json; charset=utf-8")


@get("/live/assessment")
def live_assessment(handler: "Handler", _query: Query) -> None:
    """直近の自動判定を、ライブ映像の横に出す JSON で返す。"""
    row = handler.app.clips.assessments.latest()
    payload: dict[str, Any] | None = None
    if row:
        payload = {
            "clip_id": row["clip_id"],
            "label": "判定失敗" if row["error"] else CLIP_LABELS[row["label"]],
            "reason": row["error"] or row["reason"],
            "requested_at": row["requested_at"],
            "assessed_at": row["assessed_at"],
            "human_label": CLIP_LABELS.get(row["human_label"]),
            "location": EXCRETION_LOCATIONS.get(row["location"]),
            "human_location": EXCRETION_LOCATIONS.get(row["human_location"]),
        }
    handler.respond(200, json.dumps(payload), "application/json; charset=utf-8")


# --- 保存 ---


@post("/save")
def save_label(handler: "Handler", form: Form) -> None:
    """イベントのラベル、または手動・見逃しの記録を保存し、検知の一覧へ戻す。"""
    try:
        _save_label(handler, form.fields)
    except (ValueError, UnicodeError, TypeError):
        handler.respond(400, save_failed_page(handler.app.title))
        return
    if form.fields.get("from") == "review":
        # 確認待ちから開いたイベントは、保存したら次の件へ進む（保留でも同じ件に戻らない）。
        handler.redirect(f"{NEXT_PAGE}?after=event:{form.fields.get('id', '')}")
        return
    mode = form.fields.get("filter", "unlabeled")
    if mode not in FILTERS:
        mode = "unlabeled"
    handler.redirect(f"{EVENTS_PAGE}?" + urlencode({"filter": mode, "saved": "1"}))


def _save_label(handler: "Handler", fields: Mapping[str, str]) -> None:
    store = handler.app.store
    event_id = fields.get("id")
    manual = fields.get("action") in ("missed", "manual")
    row: EventRow | None = None
    if manual:
        start = parse_time(fields.get("start"))
        end = parse_time(fields.get("end"))
        if not start:
            raise ValueError
    else:
        row = next((r for r in store.list() if r["id"] == event_id), None)
        if row is None or fields.get("label") not in (*EXCRETION_TYPES, "true", "false", "unknown"):
            raise ValueError
        start = parse_time(fields["start"]) if "start" in fields else row["actual_start"]
        end = parse_time(fields["end"]) if "end" in fields else row["actual_end"]
    if start and end and datetime.fromisoformat(end) < datetime.fromisoformat(start):
        raise ValueError
    if manual:
        if start is None:
            raise ValueError
        store.add_excretion(
            fields.get("type", "unspecified"),
            start,
            end,
            fields.get("note", ""),
            missed=fields["action"] == "missed",
            location=fields.get("location"),
        )
        return
    if row is None or event_id is None:
        raise ValueError
    label = "unspecified" if fields["label"] == "true" else fields["label"]
    store.classify(
        event_id,
        label,
        start,
        end,
        fields.get("note", row["note"]),
        location=fields.get("location"),
    )


@post("/clips/request", "/clips/label", "/clips/confirm", "/clips/protect")
def clip_form(handler: "Handler", form: Form) -> None:
    """動画の保存要求・ラベル・確定記録・保護。処理は handler.path で分かれる。

    保護の指定は誰でもできるが、解除（削除の対象に戻す）は管理者だけができる。
    """
    unprotect = handler.path == "/clips/protect" and form.fields.get("protected") == "0"
    if unprotect and not is_admin(handler):
        handler.respond(403, admin_only_page(handler.app.title))
        return
    _clip_post(handler, form)
    if unprotect and _succeeded(handler):
        audit(handler, "clip.unprotect", form.fields.get("id", ""))


@post("/clips/benchmark")
def clip_benchmark(handler: "Handler", form: Form) -> None:
    """評価の候補とタグ。ラベル付けと同じく、ログインした人なら誰でも選べる。"""
    handle_benchmark_post(
        handler,
        handler.app.clips,
        form.fields,
        form.values.get("tag", []),
        title=handler.app.title,
    )


@post("/manage/storage/cleanup", role="admin")
def clip_cleanup(handler: "Handler", form: Form) -> None:
    """選んだ古い動画の録画ファイルを消す。"""
    _clip_post(handler, form)
    if _succeeded(handler):
        location = urlsplit(handler.response.headers["location"] if handler.response else "")
        removed = parse_qs(location.query).get("removed", ["0"])[0]
        audit(handler, "clip.cleanup", f"{removed}件")


def _clip_post(handler: "Handler", form: Form) -> None:
    handle_clip_post(
        handler, handler.app.root, form.fields, form.values.get("id", ()), title=handler.app.title
    )


def _succeeded(handler: "Handler") -> bool:
    return handler.response is not None and handler.response.status_code == 303
