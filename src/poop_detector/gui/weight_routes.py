"""家族が測った犬の体重を登録し、推移を表示する。"""

from typing import TYPE_CHECKING

from ..errors import InvalidInputError
from ..records.weights import (
    birth_date,
    parse_day,
    parse_grams,
    save_birth_date,
    save_weight,
    weights,
)
from ..timezones import local_today
from .pages import Query
from .routes import Form, first, get, post
from .templating import render_template
from .weight_chart import weight_chart

if TYPE_CHECKING:
    from .exchange import Exchange as Handler


def _render(
    handler: "Handler", *, selected: str = "", error: str = "", fields: dict[str, str] | None = None
) -> None:
    db = handler.app.store.database
    rows = weights(db)
    birth = birth_date(db)
    edit = next((row for row in rows if row.id == selected), None)
    today = local_today()
    values = (
        fields
        if fields is not None
        else {
            "id": edit.id if edit else "",
            "day": edit.day.isoformat() if edit else today.isoformat(),
            "value": str(edit.grams) if edit else "",
            "note": edit.note if edit else "",
            "unit": "g",
        }
    )
    latest = rows[-1] if rows else None
    page = render_template(
        "weights.html",
        title=handler.app.title,
        token=handler.csrf,
        today=today.isoformat(),
        birth=birth,
        rows=list(reversed(rows)),
        values=values,
        error=error,
        latest=latest,
        age=(latest.day - birth).days // 7 if latest and birth and latest.day >= birth else None,
        change=latest.grams - rows[-2].grams if latest and len(rows) > 1 else None,
        chart=weight_chart(rows),
    )
    handler.respond(400 if error else 200, page)


@get("/weight")
def weight_page(handler: "Handler", query: Query) -> None:
    _render(handler, selected=first(query, "id"))


@post("/weight")
def record_weight(handler: "Handler", form: Form) -> None:
    values = form.fields
    database = handler.app.store.database
    try:
        if values.get("action") == "profile":
            save_birth_date(database, values.get("birth", ""))
        else:
            save_weight(
                database,
                values.get("id", ""),
                parse_day(values.get("day", "")),
                parse_grams(values.get("value", ""), values.get("unit", "")),
                values.get("note", "").strip(),
            )
    except InvalidInputError as error:
        _render(
            handler,
            error=str(error),
            fields=dict(values) if values.get("action") != "profile" else None,
        )
        return
    handler.redirect("/weight")
