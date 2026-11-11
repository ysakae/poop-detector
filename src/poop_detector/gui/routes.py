"""GUI の URL ごとの処理。

ルートを足すときは、関数を1つ書いて @get または @post を付ける。
安全対策（Host 検査、閲覧トークン、POST の CSRF）は server.Handler がすべてのルートの前に行う。
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias
from urllib.parse import urlencode, urlsplit

from ..detection.roi import ROI, outline
from ..features.activity_map import ActivityLayout
from ..features.tray_registration import REGISTRATION_MESSAGES, TrayRegistration
from ..records.events import EXCRETION_LOCATIONS, EXCRETION_TYPES, EventRow
from ..video.clips import CLIP_LABELS
from .activity_page import render_activity
from .clip_pages import (
    capacity_status,
    handle_clip_post,
    render_clips,
    render_storage,
    stream_video,
)
from .pages import Query, local_time, parse_time, render, render_records, save_failed_page
from .templating import render_template

if TYPE_CHECKING:
    from .server import Handler

ASSETS = Path(__file__).with_name("static")
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


def get(*paths: str) -> Callable[[GetRoute], GetRoute]:
    """GET のルートとして登録する。"""

    def register(route: GetRoute) -> GetRoute:
        for path in paths:
            GET_ROUTES[path] = route
        return route

    return register


def post(*paths: str) -> Callable[[PostRoute], PostRoute]:
    """POST のルートとして登録する。CSRF トークンは確認済みで呼ばれる。"""

    def register(route: PostRoute) -> PostRoute:
        for path in paths:
            POST_ROUTES[path] = route
        return route

    return register


def first(query: Query, name: str, default: str = "") -> str:
    return query.get(name, [default])[0]


# --- 画面 ---


@get("/")
def top_page(handler: "Handler", query: Query) -> None:
    app = handler.app
    handler.respond(200, render(app.store, app.csrf_token, query, app.disk_mb, title=app.title))


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
    page = render_clips(app.root, app.csrf_token, query, app.disk_mb, title=app.title)
    handler.respond(200, page)


@get("/clips/storage")
def storage_page(handler: "Handler", query: Query) -> None:
    app = handler.app
    page = render_storage(app.clips, app.csrf_token, query, app.disk_mb, title=app.title)
    handler.respond(200, page)


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
        token=app.csrf_token,
        points=points,
        error=error,
        **management_status(handler),
    )


@get("/manage")
def management_page(handler: "Handler", _query: Query) -> None:
    handler.respond(200, render_management(handler))


@get("/manage/status")
def management_status_json(handler: "Handler", _query: Query) -> None:
    handler.respond(200, json.dumps(management_status(handler)), "application/json; charset=utf-8")


@post("/manage/tray")
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
    handler.redirect("/manage")


# --- 画像・動画・スクリプト ---


@get("/assets/live.js", "/assets/clips.js", "/assets/clip_storage.js", "/assets/management.js")
def asset(handler: "Handler", _query: Query) -> None:
    # 許可したパスだけが登録されているので、ファイル名はこの中のどれか。
    script = ASSETS / urlsplit(handler.path).path.rsplit("/", 1)[1]
    handler.respond(200, script.read_bytes(), "text/javascript; charset=utf-8")


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
    """イベントのラベル、または手動・見逃しの記録を保存し、トップへ戻す。"""
    try:
        _save_label(handler, form.fields)
    except (ValueError, UnicodeError, TypeError):
        handler.respond(400, save_failed_page(handler.app.title))
        return
    mode = form.fields.get("filter", "unlabeled")
    if mode not in FILTERS:
        mode = "unlabeled"
    handler.redirect("/?" + urlencode({"filter": mode, "saved": "1"}))


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


@post("/clips/request", "/clips/label", "/clips/confirm", "/clips/protect", "/clips/cleanup")
def clip_form(handler: "Handler", form: Form) -> None:
    """動画の保存要求・ラベル・確定記録・保護・整理。処理は handler.path で分かれる。"""
    handle_clip_post(
        handler, handler.app.root, form.fields, form.values.get("id", ()), title=handler.app.title
    )
