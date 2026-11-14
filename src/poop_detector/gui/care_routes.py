"""家族のお世話を記録し、同じ日の体重・排泄と一緒に表示する。"""

from contextlib import suppress
from datetime import date
from typing import TYPE_CHECKING

from ..errors import InvalidInputError
from ..records.care import (
    KINDS,
    CareEntry,
    care_time,
    entries,
    feeding_history,
    format_grams,
    parse_amount,
    save_entry,
)
from ..records.events import EXCRETION_TYPES
from ..records.export import in_period
from ..records.weights import parse_day, weights
from ..timezones import local_now, local_today
from .pages import Query, local_time
from .routes import Form, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler


def _render(
    handler: "Handler",
    day: date,
    selected: str = "",
    error: str = "",
    submitted: dict[str, str] | None = None,
) -> None:
    store = handler.app.store
    rows = entries(store.database, day)
    edit = next((row for row in rows if row.id == selected), None)
    stamp = edit.at if edit else local_now()
    records = in_period(store.records(), day, day)
    food, grams, food_history = feeding_history(store.database)
    if edit:
        food, grams = edit.food, edit.grams
    kinds = (edit.kind,) if edit else tuple(KINDS)
    amounts = [row.amount_dg for row in rows if row.amount_dg is not None]
    page = render_template(
        "care.html",
        title=handler.app.title,
        token=handler.csrf,
        day=day.isoformat(),
        today=local_today().isoformat(),
        rows=rows,
        kinds=KINDS,
        form_kinds=kinds,
        food=food,
        grams=grams,
        food_history=food_history,
        submitted=submitted or {},
        recorded_grams=format_grams(sum(amounts)) if amounts else "",
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
    day = local_today()
    try:
        stamp = care_time(values.get("at", ""))
        day = min(day, stamp.astimezone().date())
        kind = values.get("kind", "")
        entry = CareEntry(
            values.get("id", ""),
            stamp,
            kind,
            user.name,
            values.get("note", "").strip(),
            values.get("food", "") if kind == "feeding" else "",
            parse_amount(values.get("grams", "")) if kind == "feeding" else None,
        )
        save_entry(handler.app.store.database, entry)
    except InvalidInputError as error:
        with suppress(InvalidInputError):
            day = parse_day(values.get("view_day", ""))
        _render(handler, day, values.get("id", ""), str(error), dict(values))
        return
    handler.redirect(f"/care?day={stamp.astimezone().date().isoformat()}")
