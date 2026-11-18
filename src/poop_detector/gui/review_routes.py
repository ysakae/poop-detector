"""確認待ちの画面と、次の件へ進む転送。

ラベルを付ける画面はホーム（検知イベント）と動画の画面をそのまま使う。from=review で開くと、
保存したあとに /review/next へ戻り、次の件を開く。ルートの表への登録はこのモジュールを
読み込んだときに行う（asgi.py が読み込む）。

画角を変える前の動画などは、画角ごとにまとめて確認待ちから外せる（/review/dismiss）。
外すだけでラベルは付けず、確認待ちへ戻せる（/review/restore）。
"""

from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from .clip_pages import SOURCES, clip_assessment_summary
from .list_time import by_day, list_time
from .pages import Query
from .review_queue import (
    NEXT_PAGE,
    ReviewItem,
    SceneGroup,
    find_scene,
    item_time,
    next_item,
    review_items,
    scene_groups,
    scene_key,
)
from .routes import Form, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

REVIEW_PAGE = "/review"


def _entry(item: ReviewItem) -> dict[str, Any]:
    """一覧の1行。動画の一覧（clip_pages.clip_list_item）と同じ見せ方にそろえる。"""
    if item.kind == "event":
        what = "差分の検知（画像）"
        thumbnail = f"/image?id={item.id}" if item.image else ""
    else:
        what = (
            "差分の検知（動画）"
            if item.event_id
            else f"{SOURCES.get(item.source, '保存要求')}の動画"
        )
        thumbnail = f"/clip-thumbnail?id={item.id}"
    return {
        "when": list_time(item.at),
        "what": what,
        "kind": item.kind,
        "assessment": clip_assessment_summary(item.review_label, item.location, item.review_failed),
        "thumbnail": thumbnail,
        "icon": "camera" if item.kind == "event" else "video",
        "url": item.url,
    }


DISMISS_PAGE = "/review/dismiss"
RESTORE_PAGE = "/review/restore"


def _scene(group: SceneGroup) -> dict[str, Any]:
    """画角ごとの確認待ちの1行。"""
    key = scene_key(group.scene)
    return {
        "key": key,
        "waiting": group.waiting,
        "dismissed": group.dismissed,
        "first": list_time(group.first),
        "last": list_time(group.last),
        "thumbnail": f"/clip-thumbnail?id={group.thumbnail}" if group.thumbnail else "",
        "current": group.current,
        "known": group.scene is not None,
        "dismiss_url": f"{DISMISS_PAGE}?scene={key}",
    }


def _scene_groups(handler: "Handler") -> tuple[list[ReviewItem], list[SceneGroup]]:
    app = handler.app
    items = review_items(app.store, app.clips)
    return items, scene_groups(items, app.clips.list(include_assessments=True))


def _count(query: Query, name: str) -> int:
    value = first(query, name)
    return int(value) if value.isdecimal() else 0


@get(REVIEW_PAGE)
def review_page(handler: "Handler", query: Query) -> None:
    app = handler.app
    items, groups = _scene_groups(handler)
    entries = [_entry(item) for item in items]
    scenes = [_scene(group) for group in groups]
    page = render_template(
        "review.html",
        title=app.title,
        count=len(entries),
        groups=by_day(entries),
        done=first(query, "done") == "1",
        end=first(query, "end") == "1",
        next_url=NEXT_PAGE,
        # 画角が1つで外した動画もなければ、まとめて外す欄は出さない。
        scenes=scenes if len(scenes) > 1 or any(s["dismissed"] for s in scenes) else [],
        dismissed=_count(query, "dismissed"),
        restored=_count(query, "restored"),
        token=handler.csrf,
        restore_url=RESTORE_PAGE,
    )
    handler.respond(200, page)


@get(DISMISS_PAGE)
def review_dismiss_page(handler: "Handler", query: Query) -> None:
    """画角の確認待ちの動画をまとめて外す前の確認。"""
    _, groups = _scene_groups(handler)
    group = find_scene(groups, first(query, "scene"))
    if group is None or not group.waiting:
        handler.respond(404, "確認待ちの動画が見つかりません")
        return
    page = render_template(
        "review_dismiss.html",
        title=handler.app.title,
        scene=_scene(group),
        token=handler.csrf,
        action=DISMISS_PAGE,
    )
    handler.respond(200, page)


@post(DISMISS_PAGE)
def review_dismiss(handler: "Handler", form: Form) -> None:
    """確認待ちから外す。scene は画角の確認待ちの動画すべて、id は動画1本。

    id で外したとき、確認待ちから開いていれば（from=review）次の件へ進む。
    """
    clips = handler.app.clips
    clip_id = form.fields.get("id", "")
    if clip_id:
        clips.dismissals.dismiss([clip_id])
        if form.fields.get("from") == "review":
            handler.redirect(f"{NEXT_PAGE}?after=clip:{clip_id}")
        else:
            handler.redirect("/clips?" + urlencode({"id": clip_id}))
        return
    _, groups = _scene_groups(handler)
    group = find_scene(groups, form.fields.get("scene", ""))
    if group is None or not group.waiting:
        handler.respond(404, "確認待ちの動画が見つかりません")
        return
    count = clips.dismissals.dismiss(group.clip_ids)
    handler.redirect(f"{REVIEW_PAGE}?dismissed={count}")


@post(RESTORE_PAGE)
def review_restore(handler: "Handler", form: Form) -> None:
    """確認待ちへ戻す。scene は画角の外した動画すべて、id は動画1本。"""
    clips = handler.app.clips
    clip_id = form.fields.get("id", "")
    if clip_id:
        clips.dismissals.restore([clip_id])
        handler.redirect("/clips?" + urlencode({"id": clip_id}))
        return
    key = form.fields.get("scene", "")
    ids = [
        row["id"]
        for row in clips.list(include_assessments=True)
        if row["review_dismissed"] and scene_key(row["scene_signature"]) == key
    ]
    count = clips.dismissals.restore(ids)
    handler.redirect(f"{REVIEW_PAGE}?restored={count}")


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
