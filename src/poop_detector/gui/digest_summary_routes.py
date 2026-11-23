"""留守中のまとめの要約を頼む（作り直す）POST と、画面に出す要約の値。"""

from typing import TYPE_CHECKING, Any

from ..config import Config
from ..records.away_periods import AwayPeriods
from ..records.digest_summaries import DigestSummaries
from .routes import Form, post

if TYPE_CHECKING:
    from .exchange import Exchange as Handler
    from .server import GuiApp


def summary_enabled(app: "GuiApp") -> bool:
    config = Config.load(app.config_path) if app.config_path is not None else Config()
    return config.digest_summary


def summary_view(app: "GuiApp", period_id: str, finished: bool) -> dict[str, Any] | None:
    """まとめの画面に出す要約の値。要約を使わない設定なら None。"""
    if not summary_enabled(app):
        return None
    summary = DigestSummaries(app.store.database).get(period_id)
    return {
        "status": summary.status if summary else None,
        "summary": summary.summary if summary else "",
        "points": summary.points if summary else [],
        # 外出中は、終わってから自動で作る。
        "can_request": finished and (summary is None or summary.status in ("done", "failed")),
    }


@post("/digest/summary")
def request_summary(handler: "Handler", form: Form) -> None:
    """外出のまとめの要約を頼み、まとめの画面へ戻す。作るのは GUI のサーバーの見張り。"""
    period_id = form.fields.get("id", "")
    period = AwayPeriods(handler.app.store.database).get(period_id)
    if period is None or period.ended_at is None:
        handler.respond(404, "終わった外出のまとめが見つかりません")
        return
    DigestSummaries(handler.app.store.database).request(period_id)
    handler.redirect(f"/digest?id={period_id}")
