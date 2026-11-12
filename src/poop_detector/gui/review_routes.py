"""確認待ちの画面と、次の件へ進む転送。

ラベルを付ける画面はホーム（検知イベント）と動画の画面をそのまま使う。from=review で開くと、
保存したあとに /review/next へ戻り、次の件を開く。ルートの表への登録はこのモジュールを
読み込んだときに行う（asgi.py が読み込む）。
"""

from typing import TYPE_CHECKING

from ..video.clips import CLIP_LABELS
from .clip_pages import SOURCES
from .pages import Query, local_time
from .review_queue import NEXT_PAGE, ReviewItem, item_time, next_item, review_items
from .routes import first, get
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

REVIEW_PAGE = "/review"


def _entry(item: ReviewItem) -> dict[str, str]:
    if item.kind == "event":
        what = "差分の検知（画像）"
    elif item.event_id:
        what = "差分の検知（動画）"
    else:
        what = f"{SOURCES.get(item.source, '保存要求')}の動画"
    hint = f"自動判定：{CLIP_LABELS[item.review_label]}" if item.review_label else ""
    return {"time": local_time(item.at), "what": what, "hint": hint, "url": item.url}


@get(REVIEW_PAGE)
def review_page(handler: "Handler", query: Query) -> None:
    app = handler.app
    entries = [_entry(item) for item in review_items(app.store, app.clips)]
    page = render_template(
        "review.html",
        title=app.title,
        entries=entries,
        done=first(query, "done") == "1",
        end=first(query, "end") == "1",
        next_url=NEXT_PAGE,
    )
    handler.respond(200, page)


@get(NEXT_PAGE)
def review_next(handler: "Handler", query: Query) -> None:
    """after の次の件（after がなければ先頭）を開く。

    最後まで進んだら確認待ちの画面へ戻す。飛ばした件が残っていれば end=1、なければ done=1。
    """
    app = handler.app
    items = review_items(app.store, app.clips)
    after = first(query, "after") or None
    at = item_time(app.store, app.clips, after) if after else None
    item = next_item(items, after, at)
    if item is not None:
        handler.redirect(item.url)
    else:
        handler.redirect(f"{REVIEW_PAGE}?end=1" if items else f"{REVIEW_PAGE}?done=1")
