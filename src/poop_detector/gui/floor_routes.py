"""床の候補（実験的）の一覧・画像・人のラベルを受け付ける画面。"""

from typing import TYPE_CHECKING

from ..records.floor_objects import FLOOR_LABELS, FloorObjects
from .assets import IMAGE_CACHE
from .list_time import list_time
from .pages import Query
from .routes import Form, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler
    from .server import GuiApp

FLOOR_PAGE = "/floor"


def floor_objects(app: "GuiApp") -> FloorObjects:
    return FloorObjects(app.store.database, app.root)


@get(FLOOR_PAGE)
def floor_page(handler: "Handler", query: Query) -> None:
    """床の候補を新しい順に並べ、画像を見てラベルを付けられるようにする。"""
    rows = [
        {
            "id": row.id,
            "when": list_time(row.detected_at),
            "label": FLOOR_LABELS.get(row.label or ""),
            "percent": f"{row.area_ratio:.1%}",
        }
        for row in floor_objects(handler.app).list()
    ]
    handler.respond(
        200,
        render_template(
            "floor.html",
            title=handler.app.title,
            token=handler.csrf,
            rows=rows,
            labels=FLOOR_LABELS,
            selected=first(query, "id"),
            saved=first(query, "saved") == "1",
        ),
    )


@get("/floor-image")
def floor_image(handler: "Handler", query: Query) -> None:
    """床の候補の画像（枠を描いた映像）。ID の形が違えば探さない。"""
    try:
        path = floor_objects(handler.app).image_path(first(query, "id"))
    except KeyError:
        path = None
    if path is None or not path.is_file():
        handler.respond(404, "画像が見つかりません")
        return
    handler.respond(200, path.read_bytes(), "image/jpeg", {"Cache-Control": IMAGE_CACHE})


@post("/floor/label")
def label_floor_object(handler: "Handler", form: Form) -> None:
    """床の候補にラベルを付け、一覧の同じ件へ戻す。"""
    object_id = form.fields.get("id", "")
    user = handler.user.name if handler.user is not None else ""
    try:
        floor_objects(handler.app).label(object_id, form.fields.get("label", ""), user)
    except (KeyError, ValueError):
        handler.respond(400, "床の候補かラベルが正しくありません")
        return
    handler.redirect(f"{FLOOR_PAGE}?saved=1#floor-{object_id}")
