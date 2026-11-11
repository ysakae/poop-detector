"""CLI の入口。引数を解析し、サブコマンドの処理（commands/）へ振り分ける。"""

import argparse
import logging
import sys
import traceback
from collections.abc import Sequence

import cv2

from .commands import (
    benchmark,
    diagnose,
    dog_model,
    notify,
    records,
    review,
    run,
    setup,
    show_config,
    users,
)
from .commands.context import CommandContext, Handler
from .config import Config, apply_timezone
from .errors import UserError
from .records.events import EXCRETION_TYPES
from .redaction import RedactingFormatter, install_log_redaction, redact

# 以前の名前のサブコマンド。引き続き使える。
ALIASES = {"codex-review": "video-review"}
HANDLERS: dict[str, Handler] = {
    "check-camera": setup.check_camera,
    "setup-roi": setup.setup_roi,
    "setup-activity-map": setup.setup_activity_map,
    "capture-baseline": setup.capture_baseline,
    "tray-registration": setup.tray_registration,
    "run": run.run,
    "events": records.events,
    "metrics": records.metrics,
    "test-notification": notify.test_notification,
    "video-review": review.video_review,
    "video-benchmark": benchmark.video_benchmark,
    "circling-review": review.circling_review,
    "feedback": records.feedback,
    "acknowledge": records.acknowledge,
    "missed": records.missed,
    "records": records.records,
    "record": records.record,
    "gui": records.gui,
    "setup-dog-model": dog_model.setup_dog_model,
    "check-dog": dog_model.check_dog,
    "show-config": show_config.show_config,
    "users": users.users,
    "diagnose": diagnose.diagnose,
}


def enrollment_minutes(value: str) -> int:
    minutes = int(value)
    if not 5 <= minutes <= 1440:
        raise argparse.ArgumentTypeError("5〜1440分で指定してください")
    return minutes


def gui_port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("ポート番号は0〜65535で指定してください")
    return port


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="犬のトイレの変化検知")
    result.add_argument("--config", default="config/config.yaml", help="設定ファイル")
    result.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="詳細なログと、失敗したときのトレースバックを表示（URL の認証情報は伏せる）",
    )
    commands = result.add_subparsers(dest="command", required=True)
    for name, description in [
        ("check-camera", "映像を1フレーム取得"),
        ("setup-roi", "監視領域を選択"),
        ("setup-activity-map", "行動マップ用にサークルの床を多角形で登録"),
        ("capture-baseline", "空のトイレの基準を登録"),
        ("run", "監視開始"),
        ("events", "イベント一覧"),
        ("metrics", "手動ラベルによる指標集計"),
        ("test-notification", "通知テスト"),
    ]:
        sub = commands.add_parser(name, help=description)
        if name in ("setup-roi", "setup-activity-map", "capture-baseline"):
            sub.add_argument("--replace", action="store_true", help="既存設定を明示的に更新")
        if name in ("capture-baseline", "run"):
            sub.add_argument(
                "--track-tray",
                action="store_true",
                help="トレーの縁による位置追従（features.track_tray と同じ）",
            )
        if name == "setup-roi":
            sub.add_argument("--outside", action="store_true", help="トレーと周囲の床を囲む")
            sub.add_argument(
                "--polygon", action="store_true", help="3点以上をクリックして多角形のROIを設定"
            )
        if name == "run":
            sub.add_argument("--max-frames", type=int, default=0, help="0なら無制限")
            sub.add_argument("--gui", action="store_true", help="監視と一緒にGUIを起動")
            sub.add_argument(
                "--gui-port", type=gui_port, help="GUIのポート（既定は設定の gui.port）"
            )
            sub.add_argument(
                "--no-browser", action="store_true", help="GUIをブラウザーで自動で開かない"
            )
            sub.add_argument(
                "--dog",
                action="store_true",
                help="犬の存在・出入り検出を有効化（dog.enabled と同じ）",
            )
            sub.add_argument(
                "--activity-map",
                action="store_true",
                help="サークル内の滞在・通過を集計（features.activity_map と同じ）",
            )
            sub.add_argument(
                "--video-review",
                "--codex",
                dest="video_review",
                action="store_true",
                help="保存した動画を自動判定し、根拠が見えたら通知（features.video_review と同じ）",
            )
            sub.add_argument(
                "--detect-outside",
                action="store_true",
                help="トレー外の排泄候補も検知（features.detect_outside と同じ）",
            )
            sub.add_argument(
                "--early-alert",
                action="store_true",
                help="トイレ滞在の見守りを録画完了前に通知（features.early_alert と同じ）",
            )
        if name == "test-notification":
            sub.add_argument(
                "--early-alert", action="store_true", help="見守り専用のntfyトピックをテスト"
            )
    registration = commands.add_parser("tray-registration", help="監視を止めずにトレーを登録")
    registration.add_argument("action", choices=("begin", "apply", "cancel", "status"))
    registration.add_argument("--polygon", action="store_true", help="apply 時に映像から範囲を選択")
    registration.add_argument("--roi", help="apply 時に読み込む ROI の JSON ファイル")
    review = commands.add_parser(
        "video-review", aliases=["codex-review"], help="保存済み動画を自動判定（通知なし）"
    )
    review.add_argument("--limit", type=int, default=5, help="処理上限（1〜50件）")
    review.add_argument("--clip", help="判定する未ラベル動画のID")
    review.add_argument("--track-tray", action="store_true", help="動画内でトレーの位置に追従")
    review.add_argument("--detect-outside", action="store_true", help="登録した周辺範囲も判定")
    compare = commands.add_parser("video-benchmark", help="ラベル付き動画で判定器・モデルを比較")
    compare.add_argument("--dataset", required=True, help="評価する録画IDとタグを指定したJSON")
    compare.add_argument(
        "--judge", required=True, action="append", help="判定器:モデル名（複数指定）"
    )
    compare.add_argument("--output", required=True, help="結果を保存する新しいJSONファイル")
    compare.add_argument("--scene", help="過去のROIと基準画像を入れたディレクトリ")
    compare.add_argument("--track-tray", action="store_true", help="トレーの位置に追従")
    compare.add_argument("--detect-outside", action="store_true", help="登録した周辺範囲も判定")
    compare.add_argument(
        "--send-images", action="store_true", help="選んだ判定器への評価画像の送信を許可"
    )
    circling = commands.add_parser("circling-review", help="保存動画の旋回候補を分析（通知なし）")
    circling.add_argument("--limit", type=int, default=5, help="処理上限（1〜50件）")
    circling.add_argument("--clip", help="分析する動画のID。指定した動画は再分析できます")
    circling.add_argument("--track-tray", action="store_true", help="動画内でトレーの位置に追従")
    feedback = commands.add_parser("feedback", help="実際に排泄だったかと種類を記録")
    feedback.add_argument("event_id")
    feedback.add_argument("label", choices=["true", "false"])
    feedback.add_argument("--start", help="実際の開始時刻（タイムゾーン付きISO8601）")
    feedback.add_argument("--end", help="実際の完了時刻（タイムゾーン付きISO8601）")
    feedback.add_argument("--type", choices=EXCRETION_TYPES, help="排泄の種類")
    feedback.add_argument("--note", help="記録のメモ")
    ack = commands.add_parser("acknowledge", help="飼い主の確認済みを記録")
    ack.add_argument("event_id")
    missed = commands.add_parser("missed", help="見逃した排便・排尿を記録")
    missed.add_argument("--start", required=True)
    missed.add_argument("--end")
    missed.add_argument("--type", choices=EXCRETION_TYPES, default="unspecified")
    commands.add_parser("records", help="確認済みの排便・排尿記録")
    record = commands.add_parser("record", help="目視で確認した排便・排尿を記録")
    record.add_argument("--type", required=True, choices=EXCRETION_TYPES)
    record.add_argument("--start", required=True)
    record.add_argument("--end")
    record.add_argument("--note", default="")
    gui = commands.add_parser("gui", help="記録・ラベル付け・管理のGUIを起動")
    gui.add_argument("--port", type=gui_port, help="GUIのポート（既定は設定の gui.port）")
    gui.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    commands.add_parser("setup-dog-model", help="犬検出モデルを固定版・ハッシュ確認付きで取得")
    dog_check = commands.add_parser("check-dog", help="静止画で犬検出と処理負荷を確認")
    dog_check.add_argument("--image", required=True)
    dog_check.add_argument("--repeat", type=int, default=10)
    commands.add_parser("show-config", help="設定ファイルから読んだ有効な設定を表示")
    commands.add_parser(
        "diagnose", help="不具合の報告に貼る診断情報を表示（URL・トピック名・パスは含まない）"
    )
    accounts = commands.add_parser(
        "users", help="GUI のユーザーの一覧・登録用 URL・復旧・無効化（このマシンで実行）"
    )
    accounts.add_argument("action", choices=("list", "invite", "recover", "disable", "enable"))
    accounts.add_argument("name", nargs="?", help="ユーザーの名前")
    accounts.add_argument(
        "--role", choices=("admin", "member"), default="member", help="invite の役割"
    )
    accounts.add_argument(
        "--minutes", type=enrollment_minutes, default=30, help="登録用 URL の有効時間（5〜1440分）"
    )
    for name, sub in commands.choices.items():
        sub.set_defaults(handler=HANDLERS[ALIASES.get(name, name)])
    return result


def configure_logging(verbose: bool) -> None:
    """ログの形式と詳細度を決め、どの出力にも URL の認証情報を出さないようにする。"""
    install_log_redaction()
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def report_failure(message: str, verbose: bool) -> None:
    """失敗の文言を標準エラーに出す。--verbose ならトレースバックも伏せ字にして添える。"""
    print(redact(message), file=sys.stderr)
    if verbose:
        print(redact(traceback.format_exc()), end="", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI の入口。終了コードを返す（成功 0、入力や設定の誤り 2、想定外の誤り 1）。

    外部ライブラリの例外の文字列は RTSP の URL を含みうるので表示せず、種類だけを伝える。
    """
    args = parser().parse_args(argv)
    configure_logging(args.verbose)
    try:
        config = Config.load(args.config)
        apply_timezone(config.timezone)
        handler: Handler = args.handler
        return handler(args, CommandContext(config))
    except UserError as error:
        # このパッケージで書いた文言なので、そのまま（念のため伏せ字にして）表示する。
        report_failure(str(error), args.verbose)
        return 2
    except (ValueError, OSError, cv2.error) as error:
        report_failure(
            f"処理できませんでした（{type(error).__name__}）。"
            "設定・登録済みROI・基準画像・入力値を確認してください。"
            "--verbose を付けると詳細を表示します",
            args.verbose,
        )
        return 2
    except Exception as error:
        # プログラムの誤りは入力の誤りと区別し、報告に使える種類を伝える。
        report_failure(
            f"想定外のエラー（{type(error).__name__}）で終了しました。"
            "不具合の可能性があります。--verbose の出力を添えて報告してください",
            args.verbose,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
