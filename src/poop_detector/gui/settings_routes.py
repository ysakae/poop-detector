"""管理 › 設定。設定ファイルの値を config.SETTINGS の表から並べ、確認してから保存する。

項目ごとの手書きの画面は作らない。検証は CLI と同じ Config と check_monitor_features を使い、
保存とコメントの保持、直前の設定への復元は settings_file が受け持つ。接続情報（.env）は
「設定あり・未設定」だけを出し、値は画面・ログ・監査ログに出さない。
ルートの表への登録はこのモジュールを読み込んだときに行う（asgi.py が読み込む）。
"""

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import (
    SETTINGS,
    THEME_NAMES,
    Config,
    ConfigError,
    Setting,
    load_connection_settings,
)
from ..settings_file import (
    READ_ONLY,
    SettingsConflictError,
    changes,
    digest,
    has_previous,
    parse_form,
    restore,
    write,
)
from .pages import Query, message_page
from .routes import Form, audit, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

PAGE = "/manage/settings"
SECTION_TITLES = {
    "": "全般",
    "features": "機能",
    "camera": "カメラ",
    "detection": "検知",
    "notifications": "通知",
    "clips": "動画",
    "dog": "犬の検出",
    "early_alert": "見守り通知",
    "review": "動画の自動判定",
    "gui": "GUI",
}
CHOICES = {"review_provider": ("codex", "claude", "gemini"), "gui_theme": THEME_NAMES}
CONNECTION_LABELS = {
    "POOP_RTSP_URL": "カメラの RTSP URL",
    "POOP_NTFY_TOPIC": "ntfy の通知トピック",
    "POOP_NTFY_EARLY_TOPIC": "見守り通知の ntfy トピック",
    "POOP_NTFY_SERVER": "ntfy のサーバー",
}


def _shown(value: Any) -> str:
    """画面に出す値。真偽値は YAML と同じ書き方にする。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _field(setting: Setting, value: Any) -> dict[str, Any]:
    if setting.attr in READ_ONLY:
        kind = "readonly"
    elif setting.kind is bool:
        kind = "bool"
    elif setting.attr in CHOICES:
        kind = "select"
    elif setting.kind in (int, float):
        kind = "number"
    else:
        kind = "text"
    return {
        "attr": setting.attr,
        "name": setting.name,
        "rule": setting.rule,
        "kind": kind,
        "value": value if kind == "bool" else _shown(value),
        "step": "1" if setting.kind is int else "any",
        "options": CHOICES.get(setting.attr, ()),
    }


def _sections(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    """設定の表の順に、セクションごとの入力欄の値を並べる。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for setting in SETTINGS:
        grouped.setdefault(setting.section, []).append(_field(setting, values[setting.attr]))
    return [
        {"title": SECTION_TITLES.get(section, section), "fields": fields}
        for section, fields in grouped.items()
    ]


def _submitted(form: Form, current: Config) -> dict[str, Any]:
    """エラーで表示し直すときの値。入力した文字列をそのまま残す。"""
    values: dict[str, Any] = {}
    for setting in SETTINGS:
        if setting.attr in READ_ONLY:
            values[setting.attr] = getattr(current, setting.attr)
        elif setting.kind is bool:
            values[setting.attr] = form.fields.get(setting.attr) == "true"
        else:
            values[setting.attr] = form.fields.get(setting.attr, "")
    return values


def _connections() -> list[tuple[str, bool]]:
    """接続情報が設定してあるかだけを返す（値は返さない）。"""
    values = load_connection_settings()
    return [(label, bool(values.get(key))) for key, label in CONNECTION_LABELS.items()]


def _render(
    handler: "Handler",
    path: Path | None,
    *,
    status: int = 200,
    values: Mapping[str, Any] | None = None,
    error: str | None = None,
    notice: str | None = None,
    pending: list[dict[str, str]] | None = None,
) -> None:
    current = Config.load(path) if path is not None else Config()
    page = render_template(
        "settings.html",
        title=handler.app.title,
        token=handler.csrf,
        editable=path is not None,
        # サーバーのディレクトリの場所は出さず、ファイル名だけを示す。
        file=path.name if path is not None else "",
        base=digest(path) if path is not None else "",
        sections=_sections(values or vars(current)),
        connections=_connections(),
        previous=path is not None and has_previous(path),
        error=error,
        notice=notice,
        pending=pending,
    )
    handler.respond(status, page)


def _summary(found: list[Any]) -> str:
    return "、".join(f"{c.name}: {_shown(c.before)} → {_shown(c.after)}" for c in found)


@get(PAGE, role="admin")
def settings_page(handler: "Handler", query: Query) -> None:
    path = handler.app.config_path
    notice = None
    if first(query, "saved") == "1":
        notice = "保存しました。監視と GUI を起動し直すと反映されます。"
    elif first(query, "restored") == "1":
        notice = "直前の設定に戻しました。監視と GUI を起動し直すと反映されます。"
    try:
        _render(handler, path, notice=notice)
    except ConfigError as error:
        # 手で書いた設定ファイルが読めないときは、編集できない理由を示す。
        page = message_page(
            "設定ファイルを読めません",
            str(error),
            "/manage",
            "管理へ戻る",
            title=handler.app.title,
            wrap_link=True,
        )
        handler.respond(400, page)


@post(PAGE, role="admin")
def save_settings(handler: "Handler", form: Form) -> None:
    """action が preview なら変更を確かめ、save なら保存し、restore なら直前の設定に戻す。"""
    path = handler.app.config_path
    if path is None:
        handler.respond(400, "設定ファイルの場所が分からないため、保存できません")
        return
    action = form.fields.get("action", "")
    base = form.fields.get("base", "")
    current = Config.load(path)
    try:
        if action == "restore":
            if form.fields.get("confirm_restore") != "1":
                raise ConfigError("直前の設定に戻すことを確認してください")
            restore(path, base=base)
            audit(handler, "settings.restore", path.name)
            handler.redirect(f"{PAGE}?restored=1")
            return
        if action not in ("preview", "save"):
            raise ConfigError("操作を確認してください")
        config = parse_form(form.fields, current)
        found = changes(current, config)
        if not found:
            _render(handler, path, notice="変更はありません。")
            return
        if action == "preview":
            pending = [
                {"name": c.name, "before": _shown(c.before), "after": _shown(c.after)}
                for c in found
            ]
            _render(handler, path, values=_submitted(form, current), pending=pending)
            return
        write(path, config, base=base)
    except SettingsConflictError as error:
        _render(handler, path, status=409, error=str(error))
        return
    except ConfigError as error:
        _render(handler, path, status=400, values=_submitted(form, current), error=str(error))
        return
    audit(handler, "settings.save", _summary(found))
    handler.redirect(f"{PAGE}?saved=1")
