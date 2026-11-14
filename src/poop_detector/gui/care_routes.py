"""家族のお世話を記録し、同じ日の体重・排泄と一緒に表示する。"""

from datetime import date
from typing import TYPE_CHECKING

from ..errors import InvalidInputError
from ..records.care import KINDS, CareEntry, care_time, entries, save_entry
from ..records.events import EXCRETION_TYPES
from ..records.export import in_period
from ..records.weights import parse_day, weights
from ..timezones import local_now, local_today
from .pages import Query, local_time
from .routes import Form, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler


def _render(handler: "Handler", day: date, selected: str = "", error: str = "") -> None:
    store = handler.app.store
    rows = entries(store.database, day)
    edit = next((row for row in rows if row.id == selected), None)
    stamp = edit.at if edit else local_now()
    records = in_period(store.records(), day, day)
    page = render_template(
        "care.html",
        title=handler.app.title,
        token=handler.csrf,
        day=day.isoformat(),
        today=local_today().isoformat(),
        rows=rows,
        kinds=KINDS,
        edit=edit,
        error=error,
        at=stamp.strftime("%Y-%m-%dT%H:%M:%S"),
        measurements=weights(store.database, day, day),
        excretion=[
            (
                local_time(row["actual_start"] or row["timestamp"]),
                EXCRETION_TYPES[row["excretion_type"]],
            )
            for row in records
        ],
    )
    handler.respond(400 if error else 200, page)


@get("/care")
def care_page(handler: "Handler", query: Query) -> None:
    value = first(query, "day")
    try:
        day = parse_day(value) if value else local_today()
    except InvalidInputError as error:
        _render(handler, local_today(), error=str(error))
        return
    _render(handler, day, selected=first(query, "id"))


@post("/care")
def save_care(handler: "Handler", form: Form) -> None:
    user = handler.user
    if user is None:
        handler.respond(403, "ログインしてください")
        return
    values = form.fields
    try:
        stamp = care_time(values.get("at", ""))
        entry = CareEntry(
            values.get("id", ""),
            stamp,
            values.get("kind", ""),
            user.name,
            values.get("note", "").strip(),
        )
        save_entry(handler.app.store.database, entry)
    except InvalidInputError as error:
        _render(handler, local_today(), error=str(error))
        return
    handler.redirect(f"/care?day={stamp.astimezone().date().isoformat()}")
