"""録画要求、動画のラベルと保護、録画整理のフォームを受け付ける。"""

from ..video.clip_labels import ClipLabel
from ..video.clip_storage import cleanup_selected, protect, retention_days
from .clip_pages import clips_url, handle_benchmark_post, kept_filter
from .exchange import Exchange
from .pages import message_page
from .review_queue import NEXT_PAGE
from .routes import Form, admin_only_page, audit, is_admin, post


def _saved(handler: Exchange, form: Form, clip_id: str, *, advance: bool = False) -> None:
    if advance and form.fields.get("from") == "review":
        handler.redirect(f"{NEXT_PAGE}?after=clip:{clip_id}")
    else:
        handler.redirect(clips_url(kept_filter(form.fields), clip_id, saved="1"))


def _save_error(handler: Exchange, status: int = 400) -> None:
    handler.respond(
        status,
        message_page(
            "保存できませんでした",
            "監視とカメラの接続、保存処理数、動画内の時刻を確認してください。",
            "/clips",
            "動画一覧へ戻る",
            title=handler.app.title,
        ),
    )


def _confirmation_error(handler: Exchange, error: Exception) -> None:
    handler.respond(
        400,
        message_page(
            "確定記録を保存できませんでした",
            str(error),
            "/clips",
            "動画一覧へ戻る",
            title=handler.app.title,
            wrap_link=True,
        ),
    )


@post("/clips/request")
def request_clip(handler: Exchange, form: Form) -> None:
    try:
        clip_id = handler.app.clips.request(require_live=True)
    except (ValueError, TypeError):
        _save_error(handler, 409)
        return
    _saved(handler, form, clip_id)


@post("/clips/protect")
def protect_clip(handler: Exchange, form: Form) -> None:
    value = form.fields.get("protected")
    if value == "0" and not is_admin(handler):
        handler.respond(403, admin_only_page(handler.app.title))
        return
    if value not in ("0", "1"):
        _save_error(handler)
        return
    clip_id = form.fields.get("id", "")
    try:
        protect(handler.app.clips, clip_id, value == "1")
    except (ValueError, TypeError):
        _save_error(handler)
        return
    if value == "0":
        audit(handler, "clip.unprotect", clip_id)
    _saved(handler, form, clip_id)


def _save_label(handler: Exchange, form: Form, record_id: str | None) -> str:
    fields = form.fields
    clip_id = fields.get("id", "")
    label = ClipLabel(
        fields.get("label", ""),
        float(fields["start"]) if fields.get("start") else None,
        float(fields["end"]) if fields.get("end") else None,
        fields.get("activity", "other"),
        fields.get("note", ""),
        fields.get("location"),
    )
    handler.app.clips.labels.save(clip_id, label, record_id=record_id)
    return clip_id


@post("/clips/label")
def label_clip(handler: Exchange, form: Form) -> None:
    try:
        clip_id = _save_label(handler, form, None)
    except (ValueError, TypeError):
        _save_error(handler)
        return
    _saved(handler, form, clip_id, advance=True)


@post("/clips/confirm")
def confirm_clip(handler: Exchange, form: Form) -> None:
    try:
        clip_id = _save_label(handler, form, form.fields.get("record_id", ""))
    except (ValueError, TypeError) as error:
        _confirmation_error(handler, error)
        return
    _saved(handler, form, clip_id, advance=True)


@post("/clips/quick")
def quick_clip(handler: Exchange, form: Form) -> None:
    fields = form.fields
    store = handler.app.clips
    clip_id = fields.get("id", "")
    label = fields.get("label", "")
    try:
        if label not in ("feces", "urine", "both", "none"):
            raise ValueError("排泄の種類を選んでください")
        row = store.get(clip_id)
        if row is None:
            raise ValueError("動画が見つかりません")
        if store.labels.confirmation(clip_id) or row["label"] is not None:
            _saved(handler, form, clip_id)
            return
        record_id = fields.get("record_id") or ("new" if not row["event_id"] else "")
        store.labels.save(
            clip_id,
            ClipLabel(
                label,
                activity="other" if label == "none" else "excreting",
                location=fields.get("location") or None,
            ),
            record_id=None if label == "none" else record_id,
            only_unlabeled=True,
        )
    except (ValueError, TypeError) as error:
        _confirmation_error(handler, error)
        return
    _saved(handler, form, clip_id, advance=True)


@post("/clips/benchmark")
def clip_benchmark(handler: Exchange, form: Form) -> None:
    handle_benchmark_post(
        handler, handler.app.clips, form.fields, form.values.get("tag", []), title=handler.app.title
    )


@post("/manage/storage/cleanup", role="admin")
def clip_cleanup(handler: Exchange, form: Form) -> None:
    try:
        if form.fields.get("confirm") != "1":
            raise ValueError("削除の確認欄にチェックを入れてください")
        days = retention_days(form.fields.get("days", ""))
        removed, skipped = cleanup_selected(handler.app.clips, form.values.get("id", ()), days)
    except (ValueError, TypeError) as error:
        handler.respond(
            400,
            message_page(
                "動画を整理できませんでした",
                str(error),
                "/manage/storage",
                "録画の整理へ戻る",
                title=handler.app.title,
                wrap_link=True,
            ),
        )
        return
    audit(handler, "clip.cleanup", f"{removed}件")
    handler.redirect(f"/manage/storage?days={days}&removed={removed}&skipped={skipped}")
