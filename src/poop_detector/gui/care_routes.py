"""家族のお世話を記録し、同じ日の体重・排泄と一緒に表示する。"""

from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import date
from typing import TYPE_CHECKING, Any

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
from ..records.weights import Weight, parse_day, weights
from ..timezones import local_now, local_today, to_local
from .pages import Query
from .routes import Form, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

CARE_ICONS = {"feeding": "food", "sheet": "sheet"}
EXCRETION_ICONS = {"feces": "feces", "urine": "urine"}


def _timeline(
    day: date,
    rows: Sequence[CareEntry],
    measured: Sequence[Weight],
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """その日のお世話・体重・確定した排泄を、新しい順の1本の時系列にする。

    体重は時刻を持たないので、いちばん上に置く。
    """
    items: list[tuple[str, dict[str, str]]] = []
    for row in rows:
        amount = f"{row.grams} g" if row.grams else ""
        detail = " ／ ".join(part for part in (row.food, amount, row.actor, row.note) if part)
        items.append(
            (
                row.at.strftime("%H:%M:%S"),
                {
                    "time": row.at.strftime("%H:%M"),
                    "icon": CARE_ICONS[row.kind],
                    "what": KINDS[row.kind],
                    "detail": detail,
                    "url": f"/care?day={day.isoformat()}&id={row.id}",
                },
            )
        )
    for record in records:
        at = to_local(record["actual_start"] or record["timestamp"])
        items.append(
            (
                at.strftime("%H:%M:%S"),
                {
                    "time": at.strftime("%H:%M"),
                    "icon": EXCRETION_ICONS.get(record["excretion_type"], "paw"),
                    "what": EXCRETION_TYPES[record["excretion_type"]],
                    "detail": "確定した排泄",
                    "url": "",
                },
            )
        )
    ordered = [item for _, item in sorted(items, key=lambda pair: pair[0], reverse=True)]
    weighed = [
        {
            "time": "",
            "icon": "weight",
            "what": "体重",
            "detail": " ／ ".join(part for part in (f"{row.grams:,} g", row.note) if part),
            "url": "",
        }
        for row in measured
    ]
    return weighed + ordered


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
        recorded_grams=format_grams(sum(amounts)) if amounts else "0",
        sheet_count=sum(row.kind == "sheet" for row in rows),
        timeline=_timeline(day, rows, weights(store.database, day, day), records),
        edit=edit,
        error=error,
        at=stamp.strftime("%Y-%m-%dT%H:%M:%S"),
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
