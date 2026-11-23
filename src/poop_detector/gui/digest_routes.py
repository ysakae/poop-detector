"""留守中のまとめ（外出ごとの年表とタイムラプス）の画面のルート。"""

import logging
import subprocess
from typing import TYPE_CHECKING

from ..features.digest import DigestSnapshots, timelapse
from ..records.away_periods import AwayPeriods
from .clip_pages import stream_file
from .digest_page import build_digest
from .digest_summary_routes import summary_view
from .list_time import list_time
from .pages import Query
from .routes import first, get
from .sleep_page import duration_text
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

logger = logging.getLogger(__name__)
DIGEST_PAGE = "/digest"


@get(DIGEST_PAGE)
def digest_page(handler: "Handler", query: Query) -> None:
    """?id= があればその外出のまとめ、なければ外出の一覧を出す。"""
    app = handler.app
    periods = AwayPeriods(app.store.database)
    identifier = first(query, "id")
    if not identifier:
        rows = [
            {
                "id": period.id,
                "start": list_time(period.started_at.isoformat()),
                "end": list_time(period.ended_at.isoformat()) if period.ended_at else None,
            }
            for period in periods.list()
        ]
        handler.respond(200, render_template("digest_list.html", title=app.title, rows=rows))
        return
    period = periods.get(identifier)
    if period is None:
        handler.respond(404, "留守中のまとめが見つかりません")
        return
    digest = build_digest(app, period)
    frames = len(DigestSnapshots(app.root).frames(period.id))
    handler.respond(
        200,
        render_template(
            "digest.html",
            title=app.title,
            digest=digest,
            start=list_time(period.started_at.isoformat()),
            end=list_time(digest.end.isoformat()),
            ongoing=period.ended_at is None,
            length=duration_text((digest.end - period.started_at).total_seconds() / 60),
            asleep=duration_text(digest.asleep_minutes) if digest.asleep_minutes else None,
            frames=frames,
            summary=summary_view(app, period.id, period.ended_at is not None),
            token=handler.csrf,
        ),
    )


@get("/digest-video")
def digest_video(handler: "Handler", query: Query) -> None:
    """外出の間の静止画のタイムラプス。まだないか静止画が増えていれば、ここで作る。"""
    try:
        path = timelapse(DigestSnapshots(handler.app.root), first(query, "id"))
    except KeyError:
        path = None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        logger.warning("タイムラプスを作れませんでした（%s）", type(error).__name__)
        handler.respond(503, "タイムラプスを作れませんでした。FFmpeg を確認してください")
        return
    if path is None:
        handler.respond(404, "静止画がありません")
        return
    stream_file(handler, path)
