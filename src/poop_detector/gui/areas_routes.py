"""管理 › 範囲の登録。映像の上に多角形を描き、監視範囲と基準画像・トレー外の範囲・床面を登録する。

映像は、監視が動いていればそのライブ映像、止まっていればカメラから取った1枚を使う。
画面に見せた映像を snapshots で預かり、保存にはその映像をそのまま使う。
監視範囲と基準画像は、監視が止まっているときだけここで登録する（動いている間は、
新しい基準を確かめてから切り替える「稼働状況とトレー」のトレー交換を使う）。
ルートの表への登録はこのモジュールを読み込んだときに行う（asgi.py が読み込む）。
"""

import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import cv2
import numpy as np

from ..commands.context import CommandContext
from ..commands.setup import capture_frame
from ..config import Config
from ..detection.roi import ROI, AnyROI, MaskedROI, outline
from ..detection.scene import save_baseline
from ..errors import InvalidInputError, UserError
from ..features.activity_map import LAYOUT_FILE, ActivityLayout
from ..features.tray_tracking import TrayTracker
from ..frames import Frame
from .pages import Query
from .routes import Form, audit, first, get, post
from .snapshots import Snapshot, Source
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

PAGE = "/manage/areas"
FRAME_PAGE = "/manage/areas/frame"
KINDS = {
    "roi": "監視範囲と基準画像",
    "outside": "トレー外の範囲",
    "floor": "サークルの床面",
}
PREVIEW_WAIT_SECONDS = 3


class RegistrationRefusedError(UserError):
    """いまの状態では登録できない（監視中に監視範囲を登録しようとした、など）。"""


def _context(handler: "Handler") -> CommandContext:
    app = handler.app
    config = Config.load(app.config_path) if app.config_path is not None else Config()
    return CommandContext(replace(config, data_dir=str(app.root)))


def _monitoring(handler: "Handler") -> bool:
    return handler.app.preview.monitor_status() != "offline"


def _paths(context: CommandContext) -> dict[str, Any]:
    return {
        "roi": context.roi_path,
        "outside": context.outside_roi_path,
        "floor": context.root / LAYOUT_FILE,
    }


def _points(context: CommandContext, kind: str) -> list[list[float]]:
    """登録済みの範囲の外周（0〜1 の座標）。未登録や読めないときは空。"""
    try:
        roi: AnyROI = (
            ActivityLayout.load(context.root).roi
            if kind == "floor"
            else ROI.load(_paths(context)[kind])
        )
        values: list[list[float]] = (outline(roi, (1001, 1001, 3)) / 1000).tolist()
        return values
    except (OSError, ValueError, TypeError, KeyError):
        return []


def _render(
    handler: "Handler", *, status: int = 200, error: str | None = None, saved: str = ""
) -> None:
    context = _context(handler)
    paths = _paths(context)
    monitoring = _monitoring(handler)
    kinds = [
        {"value": kind, "label": label, "registered": paths[kind].is_file()}
        for kind, label in KINDS.items()
    ]
    page = render_template(
        "areas.html",
        title=handler.app.title,
        token=handler.csrf,
        kinds=kinds,
        existing={kind: _points(context, kind) for kind in KINDS},
        monitoring=monitoring,
        # 監視中は監視範囲をここで登録しないので、最初に選ぶのはトレー外の範囲にする。
        default_kind="outside" if monitoring else "roi",
        error=error,
        saved=KINDS.get(saved, ""),
    )
    handler.respond(status, page)


@get(PAGE, role="admin")
def areas_page(handler: "Handler", query: Query) -> None:
    _render(handler, saved=first(query, "saved"))


def _monitor_frame(handler: "Handler") -> Frame | None:
    """監視のライブ映像を1枚読む。監視は要求されてから書き出すので、少し待つ。"""
    preview = handler.app.preview
    deadline = time.monotonic() + PREVIEW_WAIT_SECONDS
    while time.monotonic() < deadline:
        snapshot = preview.read()
        if snapshot is not None:
            decoded = cv2.imdecode(np.frombuffer(snapshot[0], np.uint8), cv2.IMREAD_COLOR)
            if decoded is not None:
                frame: Frame = decoded
                return frame
        time.sleep(0.1)
    return None


@get(FRAME_PAGE, role="admin")
def area_frame(handler: "Handler", _query: Query) -> None:
    """登録に使う映像を JPEG で返し、保存に使う ID を X-Snapshot-Id で渡す。"""
    source: Source
    try:
        if _monitoring(handler):
            frame = _monitor_frame(handler)
            source = "monitor"
            if frame is None:
                raise RegistrationRefusedError("監視のライブ映像を取得できません")
        else:
            frame = capture_frame(_context(handler))
            source = "camera"
    except UserError as error:
        # 文言はこのパッケージで書いたもので、カメラの URL を含まない。
        handler.respond(503, str(error), "text/plain; charset=utf-8")
        return
    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        handler.respond(503, "映像を画像にできません", "text/plain; charset=utf-8")
        return
    snapshot_id = handler.app.snapshots.put(frame, source)
    handler.respond(200, jpeg.tobytes(), "image/jpeg", {"X-Snapshot-Id": snapshot_id})


def _polygon(value: str) -> MaskedROI:
    try:
        points = json.loads(value)
    except (TypeError, ValueError):
        raise InvalidInputError("範囲の頂点を3点以上選んでください") from None
    if not isinstance(points, list):
        raise InvalidInputError("範囲の頂点を3点以上選んでください")
    try:
        return MaskedROI(points)
    except (TypeError, ValueError) as error:
        raise InvalidInputError(str(error)) from None


def _save_scene(context: CommandContext, roi: MaskedROI, frame: Frame) -> None:
    """監視範囲と空のトレーの基準画像を、同じ映像から保存する（capture-baseline と同じ形）。"""
    if context.scene_root != context.root:
        raise RegistrationRefusedError(
            "トレー交換で登録した基準を使っています。監視を起動し、トレー交換で登録し直してください"
        )
    if context.config.track_tray_enabled:
        # トレーの縁を確認できない画像は、追従の基準にしない（TrayReferenceError）。
        TrayTracker(frame, roi)
        context.tray_reference_path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(context.tray_reference_path), frame):
            raise OSError("トレー追従用の基準画像を保存できません")
    roi.save(context.roi_path)
    save_baseline(context.baseline_path, roi.crop(frame), roi)


def _register(handler: "Handler", form: Form, kind: str, snapshot: Snapshot) -> None:
    context = _context(handler)
    roi = _polygon(form.fields.get("points", ""))
    if _paths(context)[kind].is_file() and form.fields.get("confirm_replace") != "yes":
        raise InvalidInputError(
            f"{KINDS[kind]}は登録済みです。置き換える場合は確認欄にチェックしてください"
        )
    if kind == "roi":
        if _monitoring(handler) or snapshot.source != "camera":
            raise RegistrationRefusedError(
                "監視が動いている間は、「稼働状況とトレー」のトレー交換で登録してください"
            )
        if form.fields.get("confirm_empty") != "yes":
            raise InvalidInputError("空のトレーが映っていることを確認してください")
        _save_scene(context, roi, snapshot.frame)
    elif kind == "outside":
        roi.save(context.outside_roi_path)
    else:
        ActivityLayout(uuid4().hex, roi).save(context.root, snapshot.frame)


@post(PAGE, role="admin")
def register_area(handler: "Handler", form: Form) -> None:
    kind = form.fields.get("kind", "")
    if kind not in KINDS:
        _render(handler, status=400, error="登録するものを選んでください")
        return
    snapshot = handler.app.snapshots.take(form.fields.get("snapshot", ""))
    try:
        if snapshot is None:
            raise InvalidInputError(
                "映像の有効期限が切れました。映像を取り直してから登録してください"
            )
        _register(handler, form, kind, snapshot)
    except RegistrationRefusedError as error:
        _render(handler, status=409, error=str(error))
        return
    except (UserError, ValueError, OSError) as error:
        # 文言はこのパッケージ（ROI の検証など）で書いたもので、外部の URL を含まない。
        _render(
            handler,
            status=400,
            error=str(error)
            if isinstance(error, UserError)
            else ("登録できませんでした。範囲と映像を確認してください"),
        )
        return
    audit(handler, f"area.{kind}", KINDS[kind])
    handler.redirect(f"{PAGE}?saved={kind}")
