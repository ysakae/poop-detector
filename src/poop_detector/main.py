"""監視・初期設定・評価のコマンドライン。"""

import argparse
import json
import logging
import os
import platform
import sys
import time
from dataclasses import asdict
from pathlib import Path

# FFmpegの診断に認証URLが含まれることを防ぐため、OpenCV読込前に抑制する。
os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "-8"

import cv2
import psutil

from .camera import Camera, LatestCamera
from .config import Config
from .detection import Detector
from .events import EXCRETION_TYPES, Store, utcnow
from .notifications import MacNotifier
from .roi import ROI, select_polygon
from .state_machine import Machine

logger = logging.getLogger(__name__)


def save_baseline(path, frame, roi):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), frame):
        raise OSError("基準画像を保存できません")
    path.with_suffix(".json").write_text(json.dumps(asdict(roi)))


def load_baseline(path, roi):
    path = Path(path)
    if json.loads(path.with_suffix(".json").read_text()) != asdict(roi):
        raise ValueError("ROIが変わっています。基準画像を再登録してください")
    frame = cv2.imread(str(path))
    if frame is None:
        raise ValueError("基準画像がありません")
    return frame


def monitor(camera, roi, baseline, config, notifier, max_frames=0):
    detector = Detector(
        baseline, config.threshold, config.min_area, config.occupied_ratio, config.color_threshold
    )
    machine = Machine(config.verify_seconds, config.clear_seconds)
    store = Store(config.data_dir)
    # 現在有効な抑止状態のみ復元し、清掃後のEMPTYも永続化する。
    persisted = store.monitor_state()
    if persisted["state"] in ("SUSPECTED", "ACKNOWLEDGED"):
        machine.latched = True
        machine.state = persisted["state"]
    seen = 0
    generation = camera.generation
    started_at = None
    process = psutil.Process()
    process.cpu_percent()
    last_health = time.monotonic()
    try:
        while not max_frames or seen < max_frames:
            tick = time.monotonic()
            frame = camera.read()
            if camera.generation != generation:
                machine.interrupt()
                generation = camera.generation
                started_at = None
            if frame is None:
                if not getattr(camera, "healthy", False):
                    machine.interrupt()
                    started_at = None
                time.sleep(1 / config.fps)
                continue
            seen += 1
            valid_mask = roi.mask(frame.shape) if hasattr(roi, "mask") else None
            observation = detector.analyze(roi.crop(frame), valid_mask)
            if observation.kind == "candidate" and machine.started is None:
                started_at = utcnow()
            if observation.kind != "candidate":
                started_at = None
            was_latched = machine.latched
            if machine.latched and store.monitor_state()["state"] == "ACKNOWLEDGED":
                machine.acknowledge()
            if machine.update(observation.kind, time.monotonic()):
                event_id = store.record(
                    frame, observation.score, started_at, (time.monotonic() - tick) * 1000
                )
                store.set_monitor_state("SUSPECTED", event_id)
                if notifier.send():
                    store.update(event_id, notified_at=utcnow())
                logger.info("排泄の疑いを記録しました: %s", event_id)
            if was_latched and not machine.latched:
                store.set_monitor_state("EMPTY")
            if time.monotonic() - last_health >= 30:
                logger.info(
                    "状態=%s CPU=%.1f%% RSS=%.1fMiB 処理=%.1fms",
                    machine.state,
                    process.cpu_percent(),
                    process.memory_info().rss / 1024**2,
                    (time.monotonic() - tick) * 1000,
                )
                last_health = time.monotonic()
            time.sleep(max(0, 1 / config.fps - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        logger.info("監視を終了しました")
    return seen


def parser():
    result = argparse.ArgumentParser(description="ここちゃんのトイレ変化検知")
    result.add_argument("--config", default="config/config.yaml", help="設定ファイル")
    commands = result.add_subparsers(dest="command", required=True)
    for name, description in [
        ("check-camera", "映像を1フレーム取得"),
        ("setup-roi", "監視領域を選択"),
        ("capture-baseline", "空のトイレの基準を登録"),
        ("run", "監視開始"),
        ("events", "イベント一覧"),
        ("metrics", "手動ラベルによる指標集計"),
        ("test-notification", "通知テスト"),
    ]:
        sub = commands.add_parser(name, help=description)
        if name in ("setup-roi", "capture-baseline"):
            sub.add_argument("--replace", action="store_true", help="既存設定を明示的に更新")
        if name == "setup-roi":
            sub.add_argument(
                "--polygon", action="store_true", help="3点以上をクリックして多角形のROIを設定"
            )
        if name == "run":
            sub.add_argument("--max-frames", type=int, default=0, help="0なら無制限")
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
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.load(args.config)
        root = Path(config.data_dir)
        if args.command == "label-gui":
            from .label_gui import serve

            serve(root, args.port, not args.no_browser)
            return 0
        if args.command in (
            "events",
            "metrics",
            "feedback",
            "acknowledge",
            "missed",
            "record",
            "records",
        ):
            store = Store(root)
            from datetime import datetime

            for field in ("start", "end"):
                value = getattr(args, field, None)
                if value and datetime.fromisoformat(value).utcoffset() is None:
                    raise ValueError("時刻には+09:00などのタイムゾーンが必要です")
            if args.command == "events":
                print(json.dumps(store.list(), ensure_ascii=False, indent=2))
            elif args.command == "metrics":
                print(json.dumps(store.metrics(), ensure_ascii=False, indent=2))
            elif args.command == "feedback":
                values = {
                    key: value
                    for key, value in [
                        ("start", args.start),
                        ("end", args.end),
                        ("note", args.note),
                    ]
                    if value is not None
                }
                existing = next(
                    (r["excretion_type"] for r in store.list() if r["id"] == args.event_id),
                    "unspecified",
                )
                store.classify(
                    args.event_id,
                    (args.type or existing) if args.label == "true" else "false",
                    **values,
                )
            elif args.command == "missed":
                store.add_excretion(args.type, args.start, args.end, missed=True)
            elif args.command == "record":
                store.add_excretion(args.type, args.start, args.end, args.note)
            elif args.command == "records":
                print(json.dumps(store.records(), ensure_ascii=False, indent=2))
            else:
                store.update(args.event_id, acknowledged=1)
                if store.monitor_state()["event_id"] == args.event_id:
                    store.set_monitor_state("ACKNOWLEDGED", args.event_id)
            return 0
        notifier = MacNotifier(config.notifications and platform.system() == "Darwin")
        if args.command == "test-notification":
            if not notifier.send():
                raise ValueError("通知が無効、非対応OS、または送信失敗です")
            print("通知要求を送信しました。通知センターで表示を確認してください")
            return 0
        source = os.environ.get("POOP_RTSP_URL")
        if not source:
            raise ValueError("POOP_RTSP_URLにEufyアプリのRTSP URLを設定してください")
        roi_path = root / "roi.json"
        baseline_path = root / "baseline" / "empty.png"
        if args.command == "setup-roi" and roi_path.exists() and not args.replace:
            raise ValueError("ROIは登録済みです。更新する場合は--replaceを指定してください")
        if args.command == "capture-baseline" and baseline_path.exists() and not args.replace:
            raise ValueError("基準は登録済みです。更新する場合は--replaceを指定してください")
        with Camera(source, config.timeout_ms, config.retry_seconds) as camera:
            if args.command == "run":
                roi = ROI.load(roi_path)
                baseline = load_baseline(baseline_path, roi)
                if baseline is None:
                    raise ValueError("先にcapture-baselineを実行してください")
                with LatestCamera(Camera(source, config.timeout_ms, config.retry_seconds)) as live:
                    live.wait_ready(config.timeout_ms / 1000 + 1)
                    monitor(live, roi, baseline, config, notifier, args.max_frames)
                return 0
            frame = camera.read()
            if frame is None:
                raise ValueError("映像を取得できません。EufyのRTSP設定と接続を確認してください")
            if args.command == "check-camera":
                print(f"フレーム取得成功: {frame.shape[1]}x{frame.shape[0]}")
            elif args.command == "setup-roi":
                if args.polygon:
                    roi = select_polygon(frame)
                else:
                    rect = cv2.selectROI("トイレ領域を選択してEnter、取消はEsc", frame)
                    cv2.destroyAllWindows()
                    roi = ROI.from_pixels(rect, frame.shape[1], frame.shape[0])
                roi.save(roi_path)
                print("ROIを保存しました")
            elif args.command == "capture-baseline":
                roi = ROI.load(roi_path)
                save_baseline(baseline_path, roi.crop(frame), roi)
                print("基準画像を保存しました")
        return 0
    except (ValueError, TypeError, OSError, cv2.error):
        # 外部例外の文字列に認証URLが含まれる可能性があるため出力しない。
        if not os.environ.get("POOP_RTSP_URL") and args.command in (
            "check-camera",
            "setup-roi",
            "capture-baseline",
            "run",
        ):
            print("POOP_RTSP_URLにEufyアプリのRTSP URLを設定してください", file=sys.stderr)
        else:
            print(
                "処理できませんでした。設定・登録済みROI・基準画像・入力値を確認してください",
                file=sys.stderr,
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
