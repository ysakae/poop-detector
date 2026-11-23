"""鳴き声の記録（日ごとの鳴いていた時間と、最近の区間）の画面。"""

from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from ..records.vocal_episodes import VOCAL_LABELS, VocalEpisodes
from ..timezones import local_today
from .list_time import list_time
from .pages import Query, record_filters
from .routes import Form, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

VOCAL_PAGE = "/vocal"
SHOWN_DAYS = 14
# 鳴き声のモデルの犬らしさがこの値以上なら、モデルは鳴き声とみなす（評価の比べ方）。
MODEL_THRESHOLD = 0.5


def loud_text(seconds: float) -> str:
    """鳴いていた時間の短い表示（1分未満は秒）。"""
    if seconds < 60:
        return f"{round(seconds)}秒"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}分{rest:02d}秒"


@get(VOCAL_PAGE)
def vocal_page(handler: "Handler", query: Query) -> None:
    """直近の日ごとの鳴いていた時間と、最近の区間を新しい順に出す。"""
    episodes = VocalEpisodes(handler.app.store.database)
    today = local_today()
    oldest = today - timedelta(days=SHOWN_DAYS - 1)
    totals = episodes.daily_totals(oldest, today)
    days = [
        {
            "day": day,
            "count": totals.get(day, (0, 0.0))[0],
            "loud": loud_text(totals.get(day, (0, 0.0))[1]),
        }
        for day in (today - timedelta(days=i) for i in range(SHOWN_DAYS))
    ]
    root = handler.app.root
    recent = [
        {
            "id": item.id,
            "when": list_time(item.started_at.isoformat()),
            "loud": loud_text(item.loud_seconds),
            "peak": f"{item.peak_db:.0f}",
            "score": f"{item.dog_score:.2f}" if item.dog_score is not None else "",
            "dog_like": item.dog_score is not None and item.dog_score >= MODEL_THRESHOLD,
            "top": item.top_label or "",
            "label": VOCAL_LABELS.get(item.label or "", ""),
            "clip": episodes.clip_path(root, item.id).is_file(),
        }
        for item in episodes.list(30)
    ]
    handler.respond(
        200,
        render_template(
            "vocal.html",
            title=handler.app.title,
            token=handler.csrf,
            days=days,
            recent=recent,
            today=today,
            labels=VOCAL_LABELS,
            evaluation=_evaluation(episodes),
            saved=first(query, "saved") == "1",
            record_filters=record_filters(query),
        ),
    )


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _evaluation(episodes: VocalEpisodes) -> dict[str, object] | None:
    """人のラベルと比べた、今の判断とモデルの当たり具合（ラベルがなければ None）。"""
    result = episodes.evaluation(MODEL_THRESHOLD)
    if not result.labeled:
        return None
    return {
        "labeled": result.labeled,
        "dogs": result.dogs,
        "band_precision": _percent(result.band_precision),
        "model_cases": result.model_cases,
        "model_precision": _percent(result.model_precision),
        "model_recall": _percent(result.model_recall),
        "threshold": MODEL_THRESHOLD,
    }


@get("/vocal-audio")
def vocal_audio(handler: "Handler", query: Query) -> None:
    """評価用に残した区間の音（audio.keep_days のときだけある）。"""
    try:
        identifier = int(first(query, "id"))
    except ValueError:
        identifier = -1
    path = VocalEpisodes.clip_path(handler.app.root, identifier) if identifier >= 0 else None
    if path is None or not path.is_file():
        handler.respond(404, "音が見つかりません")
        return
    handler.respond(200, path.read_bytes(), "audio/wav")


@post("/vocal/label")
def label_vocal(handler: "Handler", form: Form) -> None:
    """区間に「鳴き声」か「ほかの音」のラベルを付け、鳴き声の記録へ戻す。"""
    user = handler.user.name if handler.user is not None else ""
    try:
        identifier = int(form.fields.get("id", ""))
        VocalEpisodes(handler.app.store.database).label(
            identifier, form.fields.get("label", ""), user
        )
    except (KeyError, ValueError):
        handler.respond(400, "鳴き声の区間かラベルが正しくありません")
        return
    params = urlencode({"saved": "1", **record_filters(form.values)})
    handler.redirect(f"{VOCAL_PAGE}?{params}#vocal-{identifier}")
