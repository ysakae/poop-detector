"""CLI の入口。引数を解析し、サブコマンドの処理（commands/）へ振り分ける。"""

import argparse
import logging
import sys
from collections.abc import Sequence

import cv2

from .commands import dog_model, notify, records, review, run, setup
from .commands.context import CommandContext, CommandError, Handler
from .config import Config, apply_timezone
from .events import EXCRETION_TYPES
from .tray_tracking import TrayReferenceError

HANDLERS: dict[str, Handler] = {
    "check-camera": setup.check_camera,
    "setup-roi": setup.setup_roi,
    "setup-activity-map": setup.setup_activity_map,
    "capture-baseline": setup.capture_baseline,
    "run": run.run,
    "events": records.events,
    "metrics": records.metrics,
    "test-notification": notify.test_notification,
    "codex-review": review.codex_review,
    "circling-review": review.circling_review,
    "feedback": records.feedback,
    "acknowledge": records.acknowledge,
    "missed": records.missed,
    "records": records.records,
    "record": records.record,
    "label-gui": records.label_gui,
    "setup-dog-model": dog_model.setup_dog_model,
    "check-dog": dog_model.check_dog,
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="犬のトイレの変化検知")
    result.add_argument("--config", default="config/config.yaml", help="設定ファイル")
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
            sub.add_argument("--track-tray", action="store_true", help="トレーの縁による位置追従")
        if name == "setup-roi":
            sub.add_argument("--outside", action="store_true", help="トレーと周囲の床を囲む")
            sub.add_argument(
                "--polygon", action="store_true", help="3点以上をクリックして多角形のROIを設定"
            )
        if name == "run":
            sub.add_argument("--max-frames", type=int, default=0, help="0なら無制限")
            sub.add_argument("--dog", action="store_true", help="犬の存在・出入り検出を有効化")
            sub.add_argument(
                "--activity-map", action="store_true", help="サークル内の滞在・通過を集計"
            )
            sub.add_argument("--codex", action="store_true", help="動画のCodex判定候補で通知")
            sub.add_argument(
                "--detect-outside", action="store_true", help="トレー外の排泄候補も検知"
            )
            sub.add_argument(
                "--early-alert", action="store_true", help="トイレ滞在の見守りを録画完了前に通知"
            )
        if name == "test-notification":
            sub.add_argument(
                "--early-alert", action="store_true", help="見守り専用のntfyトピックをテスト"
            )
    review = commands.add_parser("codex-review", help="保存済み動画をCodexで判定（通知なし）")
    review.add_argument("--limit", type=int, default=5, help="処理上限（1〜50件）")
    review.add_argument("--clip", help="判定する未ラベル動画のID")
    review.add_argument("--track-tray", action="store_true", help="動画内でトレーの位置に追従")
    review.add_argument("--detect-outside", action="store_true", help="登録した周辺範囲も判定")
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
    gui = commands.add_parser("label-gui", help="画像を見ながらラベルを付ける")
    gui.add_argument("--port", type=int, default=8765, help="ローカル画面のポート")
    gui.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    commands.add_parser("setup-dog-model", help="犬検出モデルを固定版・ハッシュ確認付きで取得")
    dog_check = commands.add_parser("check-dog", help="静止画で犬検出と処理負荷を確認")
    dog_check.add_argument("--image", required=True)
    dog_check.add_argument("--repeat", type=int, default=10)
    for name, sub in commands.choices.items():
        sub.set_defaults(handler=HANDLERS[name])
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """CLI の入口。終了コードを返す（成功 0、入力や設定の誤り 2）。"""
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.load(args.config)
        apply_timezone(config.timezone)
        handler: Handler = args.handler
        return handler(args, CommandContext(config))
    except (CommandError, TrayReferenceError) as error:
        # どちらもこのパッケージで書いた文言で、RTSP の URL を含まない。
        print(error, file=sys.stderr)
        return 2
    except (ValueError, TypeError, OSError, cv2.error):
        # 外部例外の文字列に認証URLが含まれる可能性があるため出力しない。
        print(
            "処理できませんでした。設定・登録済みROI・基準画像・入力値を確認してください",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
