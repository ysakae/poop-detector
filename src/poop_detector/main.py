"""CLI の入口。引数を解析し、サブコマンドの処理（commands/）へ振り分ける。"""

import argparse
import logging
import sys
import traceback
from collections.abc import Sequence

import cv2

from .commands import (
    backup,
    benchmark,
    circling,
    datasets,
    diagnose,
    dog_model,
    notify,
    records,
    reentry,
    review,
    run,
    setup,
    show_config,
    users,
)
from .commands.context import CommandContext, Handler
from .commands.registration import CommandGroup
from .config import Config, apply_timezone
from .errors import UserError
from .records.events import EXCRETION_TYPES, RECORD_LOCATIONS
from .redaction import RedactingFormatter, install_log_redaction, redact


def enrollment_minutes(value: str) -> int:
    minutes = int(value)
    if not 5 <= minutes <= 1440:
        raise argparse.ArgumentTypeError("5〜1440分で指定してください")
    return minutes


def dog_confidence(value: str) -> float:
    try:
        confidence = float(value)
    except ValueError:
        confidence = float("nan")
    if not 0 < confidence < 1:
        raise argparse.ArgumentTypeError("0より大きく1未満の数で指定してください")
    return confidence


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
    commands = CommandGroup(result.add_subparsers(dest="command", required=True))
    _monitor_commands(commands)
    _review_commands(commands)
    _record_commands(commands)
    _management_commands(commands)
    return result


def _monitor_commands(commands: CommandGroup) -> None:
    commands.add("check-camera", setup.check_camera, help="映像を1フレーム取得")
    roi = commands.add("setup-roi", setup.setup_roi, help="監視領域を選択")
    roi.add_argument("--replace", action="store_true", help="既存設定を明示的に更新")
    roi.add_argument("--outside", action="store_true", help="トレーと周囲の床を囲む")
    roi.add_argument(
        "--polygon", action="store_true", help="3点以上をクリックして多角形のROIを設定"
    )
    activity_map = commands.add(
        "setup-activity-map",
        setup.setup_activity_map,
        help="行動マップ用にサークルの床を多角形で登録",
    )
    activity_map.add_argument("--replace", action="store_true", help="既存設定を明示的に更新")
    baseline = commands.add(
        "capture-baseline", setup.capture_baseline, help="空のトイレの基準を登録"
    )
    baseline.add_argument("--replace", action="store_true", help="既存設定を明示的に更新")
    baseline.add_argument(
        "--track-tray",
        action="store_true",
        help="トレーの縁による位置追従（features.track_tray と同じ）",
    )
    run_command = commands.add("run", run.run, help="監視開始")
    run_command.add_argument(
        "--track-tray",
        action="store_true",
        help="トレーの縁による位置追従（features.track_tray と同じ）",
    )
    run_command.add_argument("--max-frames", type=int, default=0, help="0なら無制限")
    run_command.add_argument("--gui", action="store_true", help="監視と一緒にGUIを起動")
    run_command.add_argument(
        "--gui-port", type=gui_port, help="GUIのポート（既定は設定の gui.port）"
    )
    run_command.add_argument(
        "--no-browser", action="store_true", help="GUIをブラウザーで自動で開かない"
    )
    run_command.add_argument(
        "--dog",
        action="store_true",
        help="犬の存在・出入り検出を有効化（dog.enabled と同じ）",
    )
    run_command.add_argument(
        "--activity-map",
        action="store_true",
        help="サークル内の滞在・通過を集計（features.activity_map と同じ）",
    )
    run_command.add_argument(
        "--sleep",
        action="store_true",
        help="犬の睡眠を1分ごとに記録（sleep.enabled と同じ。犬の検出を自動で有効にする）",
    )
    run_command.add_argument(
        "--video-review",
        "--codex",
        dest="video_review",
        action="store_true",
        help="保存した動画を自動判定し、根拠が見えたら通知（features.video_review と同じ）",
    )
    run_command.add_argument(
        "--detect-outside",
        action="store_true",
        help="トレー外の排泄候補も検知（features.detect_outside と同じ）",
    )
    run_command.add_argument(
        "--early-alert",
        action="store_true",
        help="トイレ滞在の見守りを録画完了前に通知（features.early_alert と同じ）",
    )
    commands.add("events", records.events, help="イベント一覧")
    commands.add("metrics", records.metrics, help="手動ラベルによる指標集計")
    notification = commands.add("test-notification", notify.test_notification, help="通知テスト")
    notification.add_argument(
        "--early-alert", action="store_true", help="見守り専用のntfyトピックをテスト"
    )
    registration = commands.add(
        "tray-registration", setup.tray_registration, help="監視を止めずにトレーを登録"
    )
    registration.add_argument("action", choices=("begin", "apply", "cancel", "status"))
    registration.add_argument("--polygon", action="store_true", help="apply 時に映像から範囲を選択")
    registration.add_argument("--roi", help="apply 時に読み込む ROI の JSON ファイル")


def _review_commands(commands: CommandGroup) -> None:
    video_review = commands.add(
        "video-review",
        review.video_review,
        aliases=["codex-review"],
        help="保存済み動画を自動判定（通知なし）",
    )
    video_review.add_argument("--limit", type=int, default=5, help="処理上限（1〜50件）")
    video_review.add_argument("--clip", help="判定する未ラベル動画のID")
    video_review.add_argument(
        "--track-tray", action="store_true", help="動画内でトレーの位置に追従"
    )
    video_review.add_argument(
        "--detect-outside", action="store_true", help="登録した周辺範囲も判定"
    )
    compare = commands.add(
        "video-benchmark", benchmark.video_benchmark, help="ラベル付き動画で判定器・モデルを比較"
    )
    compare.add_argument("--dataset", required=True, help="評価する録画IDとタグを指定したJSON")
    compare.add_argument(
        "--judge",
        required=True,
        action="append",
        help="判定器:モデル名、または 判定器:モデル名@推論の深さ（複数指定できる）",
    )
    compare.add_argument(
        "--sheet-frames", type=int, default=24, help="判定器に渡すコマ数の上限（2〜24、既定24）"
    )
    compare.add_argument(
        "--sheet-width", type=int, default=480, help="1コマの幅の上限（120〜480、既定480）"
    )
    compare.add_argument(
        "--no-difference-sheet", action="store_true", help="差分の比較画像を付けない"
    )
    compare.add_argument(
        "--output", help="結果を保存する新しいJSONファイル（既定は data/benchmarks/ の下）"
    )
    compare.add_argument("--scene", help="過去のROIと基準画像を入れたディレクトリ")
    compare.add_argument("--track-tray", action="store_true", help="トレーの位置に追従")
    compare.add_argument("--detect-outside", action="store_true", help="登録した周辺範囲も判定")
    compare.add_argument(
        "--send-images", action="store_true", help="選んだ判定器への評価画像の送信を許可"
    )
    sets = commands.add(
        "benchmark-dataset",
        datasets.benchmark_dataset,
        help="判定器の比較に使う評価セットを作成・確認・固定（送信なし）",
    )
    sets.add_argument("action", choices=("create", "check", "freeze"))
    sets.add_argument("dataset", nargs="?", help="check・freeze する評価セットのJSON")
    sets.add_argument("--name", help="create: 評価セットの名前（英数字・ハイフン・下線）")
    sets.add_argument(
        "--final-since", help="create: この日（YYYY-MM-DD、ローカル時刻）以降を最終評価用にする"
    )
    sets.add_argument("--since", help="create: この日以降の録画だけを使う")
    sets.add_argument("--until", help="create: この日までの録画だけを使う")
    sets.add_argument("--base", help="create: tags・event・exclude を引き継ぐ既存の評価セット")
    sets.add_argument(
        "--selected-only",
        action="store_true",
        help="create: 動画の画面で「評価に入れる」を選んだ録画だけを使う",
    )
    sets.add_argument(
        "--scene", action="append", help="create: 過去のROIと基準画像の場所（複数指定可）"
    )
    sets.add_argument("--version", type=int, help="freeze: 固定する版の番号（1以上）")
    sets.add_argument(
        "--output", help="書き出す新しいJSONファイル（既定は data/benchmarks/datasets/）"
    )
    circling_review = commands.add(
        "circling-review", review.circling_review, help="保存動画の旋回候補を分析（通知なし）"
    )
    circling_review.add_argument("--limit", type=int, default=5, help="処理上限（1〜50件）")
    circling_review.add_argument("--clip", help="分析する動画のID。指定した動画は再分析できます")
    circling_review.add_argument(
        "--track-tray", action="store_true", help="動画内でトレーの位置に追従"
    )
    compare = commands.add(
        "circling-compare",
        circling.circling_compare,
        help="旋回の規則と軌跡の決定木を、確認済みの動画で比較（通知なし）",
    )
    compare.add_argument("--limit", type=int, default=200, help="比べる動画の上限（1〜500件）")
    compare.add_argument("--track-tray", action="store_true", help="動画内でトレーの位置に追従")
    compare.add_argument("--features", help="保存した特徴量のJSON。犬の検出をやり直さずに比べる")
    compare.add_argument("--output", help="特徴量と結果を書き出すJSONファイル")
    compare.add_argument(
        "--dog-confidence",
        type=dog_confidence,
        help="この比較だけで使う犬検出のしきい値（既定は設定の dog.confidence）",
    )


def _record_commands(commands: CommandGroup) -> None:
    feedback = commands.add("feedback", records.feedback, help="実際に排泄だったかと種類を記録")
    feedback.add_argument("event_id")
    feedback.add_argument("label", choices=["true", "false"])
    feedback.add_argument("--start", help="実際の開始時刻（タイムゾーン付きISO8601）")
    feedback.add_argument("--end", help="実際の完了時刻（タイムゾーン付きISO8601）")
    feedback.add_argument("--type", choices=EXCRETION_TYPES, help="排泄の種類")
    feedback.add_argument("--note", help="記録のメモ")
    ack = commands.add("acknowledge", records.acknowledge, help="飼い主の確認済みを記録")
    ack.add_argument("event_id")
    missed = commands.add("missed", records.missed, help="見逃した排便・排尿を記録")
    missed.add_argument("--start", required=True)
    missed.add_argument("--end")
    missed.add_argument("--type", choices=EXCRETION_TYPES, default="unspecified")
    reentry.add_parser(commands)
    commands.add("records", records.records, help="確認済みの排便・排尿記録")
    record = commands.add("record", records.record, help="目視で確認した排便・排尿を記録")
    record.add_argument("--type", required=True, choices=EXCRETION_TYPES)
    record.add_argument("--start", required=True)
    record.add_argument("--end")
    record.add_argument("--note", default="")
    record.add_argument("--location", choices=RECORD_LOCATIONS, help="排泄した場所")


def _management_commands(commands: CommandGroup) -> None:
    gui = commands.add("gui", records.gui, help="記録・ラベル付け・管理のGUIを起動")
    gui.add_argument("--port", type=gui_port, help="GUIのポート（既定は設定の gui.port）")
    gui.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    gui.add_argument("--manual-only", action="store_true", help="カメラなしで手動記録の画面を開く")
    commands.add(
        "setup-dog-model",
        dog_model.setup_dog_model,
        help="犬検出モデルを固定版・ハッシュ確認付きで取得",
    )
    dog_check = commands.add(
        "check-dog", dog_model.check_dog, help="静止画で犬検出と処理負荷を確認"
    )
    dog_check.add_argument("--image", required=True)
    dog_check.add_argument("--repeat", type=int, default=10)
    commands.add(
        "show-config", show_config.show_config, help="設定ファイルから読んだ有効な設定を表示"
    )
    commands.add(
        "diagnose",
        diagnose.diagnose,
        help="不具合の報告に貼る診断情報を表示（URL・トピック名・パスは含まない）",
    )
    accounts = commands.add(
        "users",
        users.users,
        help="GUI のユーザーの一覧・招待・役割と表示名の変更・復旧・無効化（このマシンで実行）",
    )
    accounts.add_argument(
        "action",
        choices=(
            "list",
            "invite",
            "resend",
            "cancel-invite",
            "set-role",
            "rename",
            "recover",
            "disable",
            "enable",
        ),
    )
    accounts.add_argument(
        "identity", nargs="?", help="メールアドレス（初回登録・メール未登録のユーザーは名前）"
    )
    accounts.add_argument("--name", help="メール招待で使う表示名、rename の変更後の表示名（必須）")
    accounts.add_argument(
        "--role",
        choices=("admin", "member"),
        help="invite の役割（既定は member）、set-role の変更後の役割（必須）",
    )
    accounts.add_argument(
        "--minutes", type=enrollment_minutes, default=30, help="登録用 URL の有効時間（5〜1440分）"
    )
    archive = commands.add(
        "backup",
        backup.backup,
        help="記録と登録情報のバックアップ（ZIP）を作る（秘密の値は含めない）",
    )
    archive.add_argument("output", help="書き出す ZIP ファイル（既にあれば断る）")
    archive.add_argument("--clips", action="store_true", help="動画も含める")
    recover = commands.add(
        "restore",
        backup.restore,
        help="バックアップから戻す（元のデータは名前を変えて残す。監視を止めて実行）",
    )
    recover.add_argument("archive", help="backup や GUI で作った ZIP ファイル")
    recover.add_argument("--yes", action="store_true", help="確認を求めずに復元する")


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
