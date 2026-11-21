"""家族で共有するフードの登録と、AI へのカロリー探しの画面。"""

from typing import TYPE_CHECKING

from ..config import Config
from ..errors import InvalidInputError
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
from ..video.providers import build_judge, supports_lookup
from .pages import Query
from .routes import Form, get, post
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


def _food(food: Food) -> dict[str, object]:
    return {
        "id": food.id,
        "name": food.name,
        "kcal": format_kcal(food.kcal_dk),
        "source_url": food.source_url,
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
        "source_url": lookup.source_url,
        "note": lookup.note,
        "failure": FAILURES.get(lookup.failure or "", "探せませんでした") if lookup.failure else "",
    }


def _lookup_problem(handler: "Handler") -> str | None:
    """カロリー探しを使えない理由。使えるなら None。"""
    path = handler.app.config_path
    config = Config.load(path) if path is not None else Config()
    problem = handler.app.judge_readiness.problem(config.review_provider, config.review_model)
    if problem is not None:
        return problem
    try:
        judge = build_judge(config.review_provider, config.review_model)
    except ValueError as error:
        return str(error)
    if not supports_lookup(judge):
        return "いまの判定器はウェブ検索に対応していないため、カロリー探しは使えません。"
    return None


def _render(handler: "Handler", *, error: str = "", fields: dict[str, str] | None = None) -> None:
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
        _render(handler, error=str(error))
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
