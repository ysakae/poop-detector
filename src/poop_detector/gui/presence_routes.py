"""不在の記録（犬がサークルにいなかった区間）と、家族の「外出中」の切り替えの画面。"""

from typing import TYPE_CHECKING

from ..records.absences import ABSENCE_LABELS, Absences, AwayMode
from ..records.away_periods import AwayPeriods
from ..web_push import WebPushNotifier, push_subject
from .list_time import list_time
from .pages import Query
from .routes import Form, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .accounts import User
    from .exchange import Exchange as Handler
    from .server import GuiApp

PRESENCE_PAGE = "/presence"


def _minutes(value: float | None) -> str:
    return "続いています" if value is None else f"約{round(value)}分"


@get(PRESENCE_PAGE)
def presence_page(handler: "Handler", query: Query) -> None:
    """不在の区間を新しい順に並べ、散歩などのラベルを付けられるようにする。"""
    database = handler.app.store.database
    rows = [
        {
            "id": row.id,
            "when": list_time(row.started_at.isoformat()),
            "length": _minutes(row.minutes),
            "away": row.away,
            "interrupted": row.interrupted,
            "label": ABSENCE_LABELS.get(row.label or ""),
        }
        for row in Absences(database).list()
    ]
    handler.respond(
        200,
        render_template(
            "presence.html",
            title=handler.app.title,
            token=handler.csrf,
            rows=rows,
            labels=ABSENCE_LABELS,
            away=AwayMode(database).since() is not None,
            saved=first(query, "saved") == "1",
        ),
    )


@post("/presence/label")
def label_absence(handler: "Handler", form: Form) -> None:
    identifier = form.fields.get("id", "")
    user = handler.user.name if handler.user is not None else ""
    try:
        Absences(handler.app.store.database).label(identifier, form.fields.get("label", ""), user)
    except (KeyError, ValueError):
        handler.respond(400, "不在の記録かラベルが正しくありません")
        return
    handler.redirect(f"{PRESENCE_PAGE}?saved=1#absence-{identifier}")


def notify_return(app: "GuiApp", user: "User | None", period_id: str) -> None:
    """在宅に戻したことを、押した人以外の家族に Web Push で知らせる（ntfy には送らない）。"""
    if user is None:
        return
    notifier = WebPushNotifier(
        app.push_subscriptions, app.push_keys, subject=push_subject(app.site.public_base or "")
    )
    notifier.send_return_home(user.name, period_id, exclude_user=user.id)


@post("/away")
def set_away(handler: "Handler", form: Form) -> None:
    """家族の「外出中」を切り替え、もとの画面へ戻す。"""
    away = form.fields.get("away")
    if away not in ("1", "0"):
        handler.respond(400, "外出中か在宅かを選んでください")
        return
    user = handler.user.name if handler.user is not None else ""
    database = handler.app.store.database
    returning = away == "0" and AwayMode(database).since() is not None
    AwayMode(database).set_away(away == "1", user)
    finished = AwayPeriods(database).latest_finished() if returning else None
    if finished is not None:
        notify_return(handler.app, handler.user, finished.id)
    back = form.fields.get("back", "/")
    handler.redirect(back if back in ("/", PRESENCE_PAGE) else "/")
