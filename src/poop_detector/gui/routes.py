"""ラベル GUI の URL ごとの処理。

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

from ..activity_map import ActivityLayout
from ..clips import CLIP_LABELS
from ..events import EXCRETION_LOCATIONS, EXCRETION_TYPES, EventRow
from .activity_page import render_activity
from .clip_pages import (
    capacity_status,
    handle_clip_post,
    render_clips,
    render_storage,
    stream_video,
)
from .pages import SAVE_FAILED, Query, document, parse_time, render, render_records

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


# --- 画像・動画・スクリプト ---


@get("/assets/live.js", "/assets/clips.js", "/assets/clip_storage.js")
def asset(handler: "Handler", _query: Query) -> None:
    # 許可した3つのパスだけが登録されているので、ファイル名はこの中のどれか。
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
    """直近の Codex の判定を、ライブ映像の横に出す JSON で返す。"""
    row = handler.app.clips.latest_assessment()
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
        handler.respond(400, document(SAVE_FAILED, handler.app.title))
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
