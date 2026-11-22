"""GUI の URL ごとの処理。

ルートを足すときは、関数を1つ書いて @get または @post を付ける。管理者だけの操作は
role="admin" を付けて登録する（既定はログインした人なら誰でも使える "member"）。
安全対策（Host 検査、セッション、POST の CSRF、役割）は asgi.py がすべてのルートの前に行う。
ルートは要求ごとの Exchange を受け取り、respond・redirect・stream で応答を決める。
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias
from urllib.parse import urlencode, urlsplit

from ..config import Config
from ..detection.roi import ROI, outline
from ..features.activity_map import ActivityLayout
from ..features.tray_registration import REGISTRATION_MESSAGES, TrayRegistration
from ..records.absences import Absences, AwayMode
from ..records.events import EXCRETION_LOCATIONS, EXCRETION_TYPES, EventRow
from ..records.floor_objects import FloorObjects
from ..video.clips import CLIP_LABELS
from ..video.thumbnails import ensure_thumbnail
from .access import expired_cookie, session_ids
from .accounts import ROLES, Role
from .activity_page import render_activity
from .assets import IMAGE_CACHE, serve_asset
from .auth_api import safe_next
from .clip_pages import (
    capacity_status,
    render_clips,
    render_storage,
    stream_video,
)
from .pages import (
    DETECTED_CLIPS,
    EVENTS_PAGE,
    NEW_RECORD_PAGE,
    Query,
    local_time,
    message_page,
    outage_rows,
    parse_time,
    render_event_detail,
    render_home,
    render_new_record,
    render_records,
    save_failed_page,
)
from .passkeys import PasskeyError
from .review_queue import NEXT_PAGE, review_bar, review_items
from .sheet_page import sheet_summary
from .sleep_page import render_sleep
from .templating import render_template
from .trends_page import yesterday_notes

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
    """ホーム（ダッシュボード）。問い合わせ文字列付きの古いリンクは記録の詳細か動画の一覧へ移す。"""
    query = urlsplit(handler.path).query
    recorded = first(_query, "recorded")
    if query and set(_query) != {"recorded"}:
        # 検知の一覧がホームにあった頃の URL（/?filter=…&id=…）。
        handler.redirect(f"{EVENTS_PAGE}?{query}", status=301)
        return
    app = handler.app
    pending = len(review_items(app.store, app.clips))
    config = Config.load(app.config_path) if app.config_path is not None else Config()
    sheet = None if app.manual_only else sheet_summary(app.store.database, config)
    floor = FloorObjects(app.store.database, app.root).pending_count()
    trends = [] if app.manual_only else yesterday_notes(app)
    away = AwayMode(app.store.database).since() is not None
    absent = next((a for a in Absences(app.store.database).list(1) if a.ended_at is None), None)
    handler.respond(
        200,
        render_home(
            app.store,
            app.disk_mb,
            title=app.title,
            pending=pending,
            manual_only=app.manual_only,
            token=handler.csrf,
            recorded=recorded,
            sheet=sheet,
            floor_pending=floor,
            trend_notes=trends,
            away=away,
            absent_since=absent.started_at.isoformat() if absent else None,
        ),
    )


@get(EVENTS_PAGE)
def events_page(handler: "Handler", query: Query) -> None:
    """記録の詳細。ID のない URL（以前の検知の一覧）は、動画の一覧の差分検知へ移す。"""
    app = handler.app
    if first(query, "manual"):
        # 目視の記録の入力欄は専用の画面へ移した。古いリンクとブックマークを移す。
        handler.redirect(NEW_RECORD_PAGE, status=301)
        return
    event_id = first(query, "id")
    if not event_id:
        handler.redirect(DETECTED_CLIPS, status=301)
        return
    row = next((r for r in app.store.list() if r["id"] == event_id), None)
    if row is None:
        handler.respond(
            404,
            message_page(
                "記録が見つかりません",
                "削除されたか、URL が誤っています。",
                "/records",
                "記録に戻る",
                title=app.title,
            ),
        )
        return
    page = render_event_detail(
        app.store,
        handler.csrf,
        row,
        clip_id=app.clips.for_event(event_id),
        saved=bool(first(query, "saved")),
        title=app.title,
        review=_review_bar(handler, query, f"event:{event_id}"),
    )
    handler.respond(200, page)


@get(NEW_RECORD_PAGE)
def new_record_page(handler: "Handler", _query: Query) -> None:
    handler.respond(
        200,
        render_new_record(
            handler.csrf, title=handler.app.title, manual_only=handler.app.manual_only
        ),
    )


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
    page = render_clips(
        app.root,
        handler.csrf,
        query,
        app.disk_mb,
        title=app.title,
        queue=review_items(app.store, app.clips),
        focused=first(query, "from") == "review",
        review_enabled=app.review_enabled,
    )
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


# 変換待ちの状態を一度に確かめる動画の数の上限。画面に出る変換待ちは、ふつう数本しかない。
STATUS_LIMIT = 50
CLIP_ID = re.compile(r"[0-9a-f]{32}")


@get("/clips/status")
def clip_status(handler: "Handler", query: Query) -> None:
    """変換待ちの動画の状態を、ID と状態だけの JSON で返す（static/clip_status.js が使う）。"""
    if handler.headers.get("Sec-Fetch-Site") == "cross-site":
        handler.respond(403, "アクセスできません")
        return
    ids = [clip for clip in query.get("id", []) if CLIP_ID.fullmatch(clip)]
    found = handler.app.clips.statuses(list(dict.fromkeys(ids))[:STATUS_LIMIT])
    payload = {"clips": [{"id": clip, "status": status} for clip, status in found.items()]}
    handler.respond(200, json.dumps(payload), "application/json; charset=utf-8")


@get("/clips/capacity")
def capacity(handler: "Handler", _query: Query) -> None:
    handler.respond(200, capacity_status(handler.app.clips.root, handler.app.disk_mb))


def management_status(handler: "Handler") -> dict[str, Any]:
    app = handler.app
    status = TrayRegistration(app.root).status()
    monitor = app.preview.monitor_status()
    snapshot = app.preview.read()
    tray = app.preview.tray_state() if monitor != "offline" else ""
    return {
        "phase": status["phase"],
        "message": status["error"] or REGISTRATION_MESSAGES[status["phase"]],
        "monitor": "停止中または未確認" if monitor == "offline" else "稼働中",
        "camera": {"connected": "接続中", "searching": "再接続中"}.get(monitor, "未確認"),
        "frame_time": local_time(snapshot[1]) if snapshot else "未取得",
        "tracking": {"tracking": "追従中", "searching": "探索中", "fixed": "固定範囲"}.get(
            tray, "未確認"
        ),
        # 状態のラベルの色（ok・warn・off）。文言と同じ判断から決める。
        "states": {
            "monitor": "off" if monitor == "offline" else "ok",
            "camera": {"connected": "ok", "searching": "warn"}.get(monitor, "off"),
            "tracking": {"tracking": "ok", "fixed": "ok", "searching": "warn"}.get(tray, "off"),
        },
    }


def render_management(
    handler: "Handler", error: str | None = None, template: str = "management.html"
) -> str:
    app = handler.app
    try:
        roi = ROI.load(TrayRegistration(app.root).active_scene().roi_path)
        points = (outline(roi, (1001, 1001, 3)) / 1000).tolist()
    except (OSError, ValueError, TypeError):
        points = []
    return render_template(
        template,
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


@get("/manage/areas/tray", role="admin")
def tray_page(handler: "Handler", _query: Query) -> None:
    """トレー交換。トレーの範囲は範囲の登録で扱うので、その下に置く。"""
    handler.respond(200, render_management(handler, template="tray.html"))


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
            render_management(
                handler, "操作できません。トレーの状態と範囲を確認してください。", "tray.html"
            ),
        )
        return
    audit(handler, f"tray.{action}")
    handler.redirect("/manage/areas/tray")


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
    for session_id in session_ids(handler.headers.get("cookie"), name=handler.app.cookie_name):
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
    handler.respond(
        200,
        page,
        headers={
            "Set-Cookie": expired_cookie(secure=secure, name=handler.app.cookie_name),
            # 預けたスクリプトや画像も、ログアウトした端末から消してもらう。
            "Clear-Site-Data": '"cache"',
        },
    )


# --- 画像・動画・スクリプト ---


@get(
    "/assets/live.js",
    "/assets/clips.js",
    "/assets/clip_status.js",
    "/assets/care.js",
    "/assets/weight_chart.js",
    "/assets/clip_storage.js",
    "/assets/management.js",
    "/assets/judge_models.js",
    "/assets/areas.js",
    "/assets/push.js",
    "/assets/passkey.js",
    "/assets/menu.js",
    "/assets/busy.js",
    "/assets/advice.js",
    "/assets/notification_target.js",
)
def asset(handler: "Handler", query: Query) -> None:
    # 許可したパスだけが登録されているので、ファイル名はこの中のどれか。
    name = urlsplit(handler.path).path.rsplit("/", 1)[1]
    serve_asset(handler, name, "text/javascript; charset=utf-8", first(query, "v"))


@get("/assets/style.css")
def stylesheet(handler: "Handler", query: Query) -> None:
    """全画面のスタイル。画面ごとに埋め込まず、ブラウザーに預けてもらう。"""
    serve_asset(handler, "style.css", "text/css; charset=utf-8", first(query, "v"))


@get("/assets/paw.svg")
@get("/assets/dog.svg")
def illustration(handler: "Handler", query: Query) -> None:
    """肉球の模様と犬のイラスト（CSP の img-src 'self' で読めるように同梱する）。"""
    # 許可したパスだけが登録されているので、ファイル名はこの中のどれか。
    name = urlsplit(handler.path).path.rsplit("/", 1)[1]
    serve_asset(handler, name, "image/svg+xml", first(query, "v"))


@get("/image")
def event_image(handler: "Handler", query: Query) -> None:
    """イベントの画像。events/ の外を指す記録は返さない。"""
    store = handler.app.store
    event = next((r for r in store.list() if r["id"] == first(query, "id")), None)
    if event and event["image"]:
        path = (store.root / event["image"]).resolve()
        if path.is_relative_to((store.root / "events").resolve()) and path.is_file():
            handler.respond(200, path.read_bytes(), "image/jpeg", {"Cache-Control": IMAGE_CACHE})
            return
    handler.respond(404, "画像または記録が見つかりません")


@get("/clip-video")
def clip_video(handler: "Handler", query: Query) -> None:
    stream_video(handler, handler.app.root, first(query, "id"))


@get("/clip-thumbnail")
def clip_thumbnail(handler: "Handler", query: Query) -> None:
    """動画の一覧に出すサムネイル。まだない動画は、ここで作ってから返す。"""
    clips = handler.app.clips
    path = ensure_thumbnail(clips, clips.get(first(query, "id")))
    if path is None:
        handler.respond(404, "サムネイルがありません")
        return
    handler.respond(200, path.read_bytes(), "image/jpeg", {"Cache-Control": IMAGE_CACHE})


@get("/sleep")
def sleep_page(handler: "Handler", query: Query) -> None:
    handler.respond(200, render_sleep(handler.app, query))


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
    """イベントのラベル、または手動・見逃しの記録を保存し、記録の詳細へ戻す。"""
    try:
        _save_label(handler, form.fields)
    except (ValueError, UnicodeError, TypeError):
        handler.respond(400, save_failed_page(handler.app.title))
        return
    if form.fields.get("from") == "review":
        # 確認待ちから開いたイベントは、保存したら次の件へ進む（保留でも同じ件に戻らない）。
        handler.redirect(f"{NEXT_PAGE}?after=event:{form.fields.get('id', '')}")
        return
    if form.fields.get("action") in ("missed", "manual"):
        # 目視の記録は、足した記録が見える記録の画面へ戻す。
        handler.redirect("/records?saved=1")
        return
    # 保存した記録の詳細に戻り、保存したラベルを見せる。
    event_id = form.fields.get("id", "")
    handler.redirect(f"{EVENTS_PAGE}?" + urlencode({"id": event_id, "saved": "1"}))


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
            missed=fields["action"] == "missed" and not handler.app.manual_only,
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
