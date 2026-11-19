"""家族のお世話を記録し、同じ日の体重・排泄と一緒に表示する。"""

from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from ..errors import InvalidInputError
from ..records.care import (
    KINDS,
    CareEntry,
    care_time,
    delete_entry,
    entries,
    feeding_history,
    format_grams,
    parse_amount,
    quick_entry,
    save_entry,
)
from ..records.events import EXCRETION_TYPES
from ..records.export import in_period
from ..records.feeding_plans import plans_for_day, save_plan, stop_plan
from ..records.weights import Weight, parse_day, weights
from ..timezones import local_now, local_today, to_local
from .pages import Query, message_page
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
                    "detail": "確定記録",
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
    selected_plan: str = "",
    deleted: bool = False,
) -> None:
    store = handler.app.store
    rows = entries(store.database, day)
    edit = next((row for row in rows if row.id == selected), None)
    stamp = edit.at if edit else local_now()
    if not edit and day < stamp.date():
        stamp = datetime.combine(day, stamp.time()).astimezone()
    plans = plans_for_day(store.database, day)
    if selected_plan and all(plan.id != selected_plan for plan in plans):
        handler.respond(
            400,
            message_page(
                "このごはんの予定は終了しました",
                "お世話の画面で今の予定を確認してください。",
                f"/care?day={day.isoformat()}#feeding-plans",
                "今の予定を見る",
                title=handler.app.title,
            ),
        )
        return
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
        feeding_plans=plans,
        selected_plan=selected_plan,
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
        deleted=deleted,
        at=stamp.strftime("%Y-%m-%dT%H:%M"),
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
    _render(
        handler,
        day,
        selected=first(query, "id"),
        selected_plan=first(query, "plan"),
        deleted=first(query, "deleted") == "1",
    )


@post("/care/plans")
def update_feeding_plan(handler: "Handler", form: Form) -> None:
    fields = form.fields
    database = handler.app.store.database
    try:
        if fields.get("stop") == "1":
            stop_plan(database, fields.get("plan_id", ""))
        else:
            save_plan(database, fields.get("clock", ""), plan_id=fields.get("plan_id", ""))
    except InvalidInputError as error:
        _render(handler, local_today(), error=str(error))
        return
    handler.redirect("/care#feeding-plans")


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
        plan_id = values.get("plan_id") or None
        view_day = values.get("view_day")
        if plan_id and view_day and stamp.astimezone().date().isoformat() != view_day:
            raise InvalidInputError("ごはんの予定と実施の日付をそろえてください")
        save_entry(handler.app.store.database, entry, plan_id=plan_id)
    except InvalidInputError as error:
        with suppress(InvalidInputError):
            day = parse_day(values.get("view_day", ""))
        _render(handler, day, values.get("id", ""), str(error), dict(values))
        return
    handler.redirect(f"/care?day={stamp.astimezone().date().isoformat()}")


@post("/care/delete")
def delete_care(handler: "Handler", form: Form) -> None:
    try:
        day = delete_entry(handler.app.store.database, form.fields.get("id", ""))
    except InvalidInputError as error:
        _render(handler, local_today(), error=str(error))
        return
    handler.redirect(f"/care?day={day.isoformat()}&deleted=1")


@post("/care/quick")
def quick_care(handler: "Handler", form: Form) -> None:
    """ホームから1回押して、前回と同じ給餌かシート交換を記録する。"""
    user = handler.user
    if user is None:
        handler.respond(403, "ログインしてください")
        return
    try:
        plan_id = form.fields.get("plan_id") or None
        if plan_id and form.fields.get("plan_day") != local_today().isoformat():
            raise InvalidInputError("ホームを開き直して、今日のごはんの予定を確認してください")
        kind = "feeding" if plan_id else form.fields.get("kind", "")
        identifier = quick_entry(handler.app.store.database, kind, user.name, plan_id=plan_id)
    except InvalidInputError as error:
        page = message_page(
            "記録できませんでした", str(error), "/", "ホームへ戻る", title=handler.app.title
        )
        handler.respond(400, page)
        return
    # 前回の給餌がなければ、銘柄と量を入れる画面を開く。
    handler.redirect(f"/?recorded={identifier}" if identifier else "/care#feeding")
