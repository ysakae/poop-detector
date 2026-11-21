"""ごはんの目安・プロフィール・家族で共有する相談と、フードの登録の画面。"""

import json
from typing import TYPE_CHECKING

from ..config import Config
from ..errors import InvalidInputError
from ..features.advice.context import basis_text
from ..records.advice import ask, delete_thread, messages, retry, start_thread, thread, threads
from ..records.breeds import BREEDS, SIZE_CLASSES, SIZE_NAMES, find_breed
from ..records.foods import (
    Food,
    Lookup,
    active_foods,
    parse_kcal,
    parse_source_url,
    recent_lookups,
    request_lookup,
    save_food,
    stop_food,
    unregistered_names,
)
from ..records.profile import PetProfile, save_profile
from ..records.weights import parse_grams
from ..timezones import local_today
from ..video.providers import build_judge, supports_lookup
from .advice_page import (
    DISCLAIMER,
    gain_text,
    guide_view,
    intake_view,
    message_rows,
    snapshot,
    thread_rows,
)
from .pages import Query
from .routes import Form, first, get, is_admin, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

FOODS = "/advice/foods"
FAILURES = {
    "timeout": "時間切れ",
    "unsupported": "この判定器はウェブ検索に対応していません",
    "invalid_output": "結果を読み取れませんでした",
}
STATUSES = {
    "queued": "順番待ち",
    "running": "探しています",
    "done": "見つかりました",
    "failed": "失敗",
}


def format_kcal(kcal_dk: int) -> str:
    """0.1 kcal 単位の整数を、必要なときだけ小数1桁で表す。"""
    whole, tenth = divmod(kcal_dk, 10)
    return f"{whole}.{tenth}" if tenth else str(whole)


def _safe_url(url: str) -> str:
    """表示に使える https の URL だけを返す。ほかは空（リンクにしない）。"""
    try:
        return parse_source_url(url)
    except InvalidInputError:
        return ""


def _food(food: Food) -> dict[str, object]:
    return {
        "id": food.id,
        "name": food.name,
        "kcal": format_kcal(food.kcal_dk),
        "source_url": _safe_url(food.source_url),
    }


def _lookup(lookup: Lookup) -> dict[str, object]:
    return {
        "name": lookup.name,
        "status": lookup.status,
        "status_text": STATUSES.get(lookup.status, lookup.status),
        "done": lookup.status == "done" and lookup.kcal_dk is not None,
        "product_name": lookup.product_name,
        "maker": lookup.maker,
        "kcal": "" if lookup.kcal_dk is None else format_kcal(lookup.kcal_dk),
        "source_url": _safe_url(lookup.source_url),
        "note": lookup.note,
        "failure": FAILURES.get(lookup.failure or "", "探せませんでした") if lookup.failure else "",
    }


def _config(handler: "Handler") -> Config:
    path = handler.app.config_path
    return Config.load(path) if path is not None else Config()


def _consult_problem(handler: "Handler") -> str | None:
    """相談を使えない理由。使えるなら None。"""
    config = _config(handler)
    return handler.app.judge_readiness.problem(config.review_provider, config.review_model)


def _lookup_problem(handler: "Handler") -> str | None:
    """カロリー探しを使えない理由。使えるなら None。"""
    config = _config(handler)
    problem = _consult_problem(handler)
    if problem is not None:
        return problem
    try:
        judge = build_judge(config.review_provider, config.review_model)
    except ValueError as error:
        return str(error)
    if not supports_lookup(judge):
        return "いまの判定器はウェブ検索に対応していないため、カロリー探しは使えません。"
    return None


def _render(
    handler: "Handler",
    *,
    error: str = "",
    fields: dict[str, str] | None = None,
    lookup_name: str = "",
) -> None:
    database = handler.app.store.database
    page = render_template(
        "advice_foods.html",
        title=handler.app.title,
        token=handler.csrf,
        foods=[_food(food) for food in active_foods(database)],
        lookups=[_lookup(lookup) for lookup in recent_lookups(database)],
        names=unregistered_names(database),
        lookup_problem=_lookup_problem(handler),
        values=fields or {},
        lookup_name=lookup_name,
        error=error,
    )
    handler.respond(400 if error else 200, page)


def _user_id(handler: "Handler") -> str:
    return handler.user.id if handler.user is not None else ""


@get(FOODS)
def foods_page(handler: "Handler", _query: Query) -> None:
    _render(handler)


@post("/advice/foods/lookup")
def lookup_food(handler: "Handler", form: Form) -> None:
    try:
        request_lookup(handler.app.store.database, form.fields.get("name", ""), _user_id(handler))
    except InvalidInputError as error:
        _render(handler, error=str(error), lookup_name=form.fields.get("name", ""))
        return
    handler.redirect(FOODS)


@post(FOODS)
def register_food(handler: "Handler", form: Form) -> None:
    values = form.fields
    try:
        save_food(
            handler.app.store.database,
            name=values.get("name", ""),
            kcal_dk=parse_kcal(values.get("kcal", "")),
            source_url=parse_source_url(values.get("source_url", "").strip()),
            user_id=_user_id(handler),
            food_id=values.get("food_id", ""),
        )
    except InvalidInputError as error:
        _render(handler, error=str(error), fields=dict(values))
        return
    handler.redirect(FOODS)


@post("/advice/foods/stop")
def stop_registered_food(handler: "Handler", form: Form) -> None:
    try:
        stop_food(handler.app.store.database, form.fields.get("food_id", ""))
    except InvalidInputError as error:
        _render(handler, error=str(error))
        return
    handler.redirect(FOODS)


# --- 目安・プロフィール・相談 ---

ADVICE = "/advice"
THREAD = "/advice/thread"
PERMISSION_ERROR = "この相談を消せるのは"
NEUTERED = (("1", "済み"), ("0", "まだ"), ("", "未入力"))


def _profile_values(profile: PetProfile) -> dict[str, str]:
    neutered = "" if profile.neutered is None else str(int(profile.neutered))
    return {
        "breed": profile.breed,
        "size_class": profile.size_class or "",
        "adult_grams": "" if profile.adult_grams is None else str(profile.adult_grams),
        "neutered": neutered,
        "food_id": profile.food_id or "",
    }


def _render_advice(
    handler: "Handler",
    *,
    error: str = "",
    status: int = 200,
    profile_fields: dict[str, str] | None = None,
    ask_fields: dict[str, str] | None = None,
) -> None:
    database = handler.app.store.database
    today = local_today()
    current = snapshot(database, today)
    foods = active_foods(database)
    selected = next((food for food in foods if food.id == current.profile.food_id), None)
    food_text = (
        "" if selected is None else f"{selected.name} {format_kcal(selected.kcal_dk)} kcal/100g"
    )
    intake, average = intake_view(database, today)
    page = render_template(
        "advice.html",
        title=handler.app.title,
        token=handler.csrf,
        guide=guide_view(current.advice.guide, food_text),
        warnings=list(current.advice.warnings),
        missing=list(current.advice.missing),
        disclaimer=DISCLAIMER,
        intake=intake,
        average=average,
        gain=gain_text(current.weights, today),
        threads=thread_rows(threads(database)),
        consult_problem=_consult_problem(handler),
        ask_values=ask_fields or {},
        profile=profile_fields or _profile_values(current.profile),
        breeds=[breed.name for breed in BREEDS],
        sizes=[(size, SIZE_NAMES[size]) for size in SIZE_CLASSES],
        neutered_options=NEUTERED,
        foods=[
            {"id": food.id, "label": f"{food.name}（{format_kcal(food.kcal_dk)} kcal/100g）"}
            for food in foods
        ],
        error=error,
    )
    handler.respond(status, page)


def _render_thread(
    handler: "Handler", thread_id: str, *, error: str = "", status: int = 200, question: str = ""
) -> None:
    database = handler.app.store.database
    found = thread(database, thread_id)
    if found is None:
        handler.respond(404, "相談が見つかりません")
        return
    rows = message_rows(database, messages(database, thread_id))
    waiting = any(row["waiting"] for row in rows)
    owner = handler.user is not None and handler.user.id == found.created_by
    page = render_template(
        "advice_thread.html",
        title=handler.app.title,
        token=handler.csrf,
        thread=found,
        messages=rows,
        waiting=waiting,
        can_delete=is_admin(handler) or owner,
        consult_problem=_consult_problem(handler),
        question=question,
        error=error,
    )
    handler.respond(status, page)


def _basis(handler: "Handler") -> str:
    return basis_text(snapshot(handler.app.store.database, local_today()).advice)


@get(ADVICE)
def advice_page(handler: "Handler", _query: Query) -> None:
    _render_advice(handler)


@post("/advice/profile")
def save_profile_route(handler: "Handler", form: Form) -> None:
    values = {
        name: form.fields.get(name, "").strip()
        for name in ("breed", "size_class", "adult_grams", "neutered", "food_id")
    }
    breed = find_breed(values["breed"])
    if breed is not None and not values["size_class"] and not values["adult_grams"]:
        values = {
            **values,
            "size_class": breed.size_class,
            "adult_grams": str(breed.adult_grams),
        }
    try:
        save_profile(
            handler.app.store.database,
            breed=values["breed"],
            size_class=values["size_class"],
            adult_grams=parse_grams(values["adult_grams"], "g") if values["adult_grams"] else None,
            neutered=values["neutered"],
            food_id=values["food_id"],
        )
    except InvalidInputError as error:
        _render_advice(handler, error=str(error), status=400, profile_fields=values)
        return
    handler.redirect(ADVICE)


@post("/advice/threads")
def start_thread_route(handler: "Handler", form: Form) -> None:
    try:
        thread_id = start_thread(
            handler.app.store.database,
            title=form.fields.get("title", ""),
            question=form.fields.get("question", ""),
            user_id=_user_id(handler),
            basis=_basis(handler),
        )
    except InvalidInputError as error:
        _render_advice(handler, error=str(error), status=400, ask_fields=dict(form.fields))
        return
    handler.redirect(f"{THREAD}?id={thread_id}")


@get(THREAD)
def thread_page(handler: "Handler", query: Query) -> None:
    _render_thread(handler, first(query, "id"))


@post("/advice/ask")
def ask_route(handler: "Handler", form: Form) -> None:
    thread_id = form.fields.get("thread_id", "")
    question = form.fields.get("question", "")
    try:
        ask(
            handler.app.store.database,
            thread_id,
            question=question,
            user_id=_user_id(handler),
            basis=_basis(handler),
        )
    except InvalidInputError as error:
        _render_thread(handler, thread_id, error=str(error), status=400, question=question)
        return
    handler.redirect(f"{THREAD}?id={thread_id}")


@post("/advice/retry")
def retry_route(handler: "Handler", form: Form) -> None:
    database = handler.app.store.database
    message_id = form.fields.get("message_id", "")
    try:
        retry(database, message_id)
    except InvalidInputError as error:
        _render_advice(handler, error=str(error), status=400)
        return
    with database.connect() as db:
        row = db.execute(
            "SELECT thread_id FROM advice_messages WHERE id=?", (message_id,)
        ).fetchone()
    handler.redirect(f"{THREAD}?id={row[0]}")


@post("/advice/delete")
def delete_route(handler: "Handler", form: Form) -> None:
    thread_id = form.fields.get("thread_id", "")
    try:
        delete_thread(
            handler.app.store.database,
            thread_id,
            user_id=_user_id(handler),
            admin=is_admin(handler),
        )
    except InvalidInputError as error:
        denied = str(error).startswith(PERMISSION_ERROR)
        _render_advice(handler, error=str(error), status=403 if denied else 400)
        return
    handler.redirect(ADVICE)


@get("/advice/status")
def advice_status(handler: "Handler", query: Query) -> None:
    """会話の最後の回答の状態を JSON で返す（static/advice.js が使う）。預けない。"""
    if handler.headers.get("Sec-Fetch-Site") == "cross-site":
        handler.respond(403, "アクセスできません")
        return
    found = thread(handler.app.store.database, first(query, "thread"))
    if found is None:
        handler.respond(404, json.dumps({"state": "missing"}), "application/json; charset=utf-8")
        return
    state = found.state or "done"
    handler.respond(
        200,
        json.dumps({"state": state}),
        "application/json; charset=utf-8",
        {"Cache-Control": "no-store"},
    )
