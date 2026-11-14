"""管理 › 確認と診断。通知のテスト送信、カメラの確認、診断情報を画面から行う。

処理は CLI（test-notification・check-camera・diagnose）と同じ関数を使う。接続情報（.env）の
値は画面・ログ・監査ログに出さない。監視が動いている間は、カメラへ2本目の接続を開かず、
監視が書く状態（LivePreview）を示す。ルートの表への登録はこのモジュールを読み込んだときに行う。
"""

from dataclasses import replace
from typing import TYPE_CHECKING

from ..commands.context import CommandContext, CommandError, build_notifier
from ..commands.diagnose import diagnose_report
from ..commands.setup import capture_frame
from ..config import Config, load_connection_settings
from ..errors import CameraUnavailableError, UserError
from .pages import Query, local_time
from .routes import Form, audit, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

PAGE = "/manage/checks"


@get("/manage/setup", role="admin")
def first_setup(handler: "Handler", _query: Query) -> None:
    app = handler.app
    context = CommandContext(_config(handler))
    page = render_template(
        "first_setup.html",
        title=app.title,
        camera_configured=bool(load_connection_settings()["POOP_RTSP_URL"].strip()),
        roi_registered=context.roi_path.is_file(),
        baseline_registered=context.baseline_path.is_file(),
        monitor=app.preview.monitor_status(),
    )
    handler.respond(200, page)


class NotificationFailedError(UserError):
    """通知の送信先が受け付けなかった（設定ではなく、送信の失敗）。"""


def _config(handler: "Handler") -> Config:
    """設定ファイルの設定。データの保存先は、この GUI が使っている場所に合わせる。"""
    app = handler.app
    config = Config.load(app.config_path) if app.config_path is not None else Config()
    return replace(config, data_dir=str(app.root))


def _render(
    handler: "Handler",
    *,
    status: int = 200,
    result: str | None = None,
    error: str | None = None,
    report: str | None = None,
) -> None:
    page = render_template(
        "checks.html",
        title=handler.app.title,
        token=handler.csrf,
        result=result,
        error=error,
        report=report,
        page=PAGE,
    )
    handler.respond(status, page)


@get(PAGE, role="admin")
def checks_page(handler: "Handler", query: Query) -> None:
    report = diagnose_report(CommandContext(_config(handler))) if first(query, "diagnose") else None
    _render(handler, report=report)


def _notify(handler: "Handler", early_alert: bool) -> str:
    """通知のテストを送り、結果の文言を返す。失敗は CommandError（文言はそのまま出せる）。"""
    settings = load_connection_settings()
    notifier, uses_ntfy = build_notifier(_config(handler), settings, sends=True)
    if early_alert and not settings["POOP_NTFY_EARLY_TOPIC"].strip():
        raise CommandError("見守り用の POOP_NTFY_EARLY_TOPIC を .env に設定してください")
    if not notifier.send_test(early_alert=early_alert):
        raise NotificationFailedError("通知が無効、非対応の OS、または送信に失敗しました")
    audit(handler, "check.notification", "見守り" if early_alert else "トイレ")
    if uses_ntfy:
        return "ntfy が通知を受け付けました。購読している端末で確認してください。"
    return "通知の要求を送りました。このマシンの通知センターで確認してください。"


def _camera(handler: "Handler") -> str:
    """監視が動いていればその状態を、止まっていればカメラから1フレームを読んで結果を返す。"""
    preview = handler.app.preview
    state = preview.monitor_status()
    if state != "offline":
        snapshot = preview.read()
        shown = local_time(snapshot[1]) if snapshot else "未取得"
        connection = (
            "監視がカメラにつながっています"
            if state == "connected"
            else ("監視がカメラに接続し直しています")
        )
        return f"{connection}（最後の映像 {shown}）。監視中は、カメラへ別の接続を開きません。"
    frame = capture_frame(CommandContext(_config(handler)))
    audit(handler, "check.camera", f"{frame.shape[1]}×{frame.shape[0]}")
    return f"映像を取得できました（{frame.shape[1]}×{frame.shape[0]}）。"


@post(PAGE, role="admin")
def run_check(handler: "Handler", form: Form) -> None:
    action = form.fields.get("action", "")
    try:
        if action in ("notify", "notify_early"):
            result = _notify(handler, early_alert=action == "notify_early")
        elif action == "camera":
            result = _camera(handler)
        else:
            raise CommandError("操作を確認してください")
    except UserError as error:
        # UserError の文言はこのパッケージで書いたもので、URL などの接続情報を含まない。
        failed = isinstance(error, (NotificationFailedError, CameraUnavailableError))
        status = 502 if failed else 400
        _render(handler, status=status, error=str(error))
        return
    _render(handler, result=result)
