"""監視・初期設定・評価のコマンドライン。"""

import argparse
import hashlib
import json
import logging
import os
import platform
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

# FFmpegの診断に認証URLが含まれることを防ぐため、OpenCV読込前に抑制する。
os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "-8"

import cv2
import psutil

from .activity_map import LAYOUT_FILE, ActivityLayout, ActivityRecorder, ActivityStore
from .camera import Camera, LatestCamera
from .config import Config
from .detection import IncrementalDetector
from .early_alert import EarlyAlert
from .events import EXCRETION_TYPES, Store, utcnow
from .live_preview import LivePreview
from .notifications import MacNotifier
from .roi import ROI, select_polygon
from .state_machine import Machine
from .tray_tracking import TrayReferenceError, TrayTracker

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


def scene_signature(baseline, roi):
    return hashlib.sha256(
        baseline.tobytes() + json.dumps(asdict(roi), sort_keys=True).encode()
    ).hexdigest()


def monitor(
    camera,
    roi,
    baseline,
    config,
    notifier,
    max_frames=0,
    collector=None,
    dog_observer=None,
    codex_notifications=False,
    early_alert=False,
    tray_tracker=None,
    on_tray_state=None,
    outside_roi=None,
    activity_map=None,
):
    detector = IncrementalDetector(
        baseline, config.threshold, config.min_area, config.occupied_ratio, config.color_threshold
    )
    stay = EarlyAlert(baseline, config) if early_alert else None
    machine = Machine(config.verify_seconds, config.clear_seconds)
    store = Store(config.data_dir)
    if dog_observer is None and config.dog_enabled:
        from .dog import DogDetector, DogObserver

        try:
            model = DogDetector(
                Path(config.data_dir) / "models" / "dog-yolox", config.dog_confidence
            )
        except (OSError, ValueError):
            logger.error("犬のモデルを準備してください: uv run poop-detector setup-dog-model")
            raise
        dog_observer = DogObserver(model, config.dog_fps, config.dog_exit_seconds)
    outside_watch = None
    if outside_roi is not None:
        from .dog import OutsideWatch

        outside_watch = OutsideWatch(roi, outside_roi, config.dog_exit_seconds)
    dog_state = None
    dog_clip_times = {}
    signature = scene_signature(baseline, roi)
    if collector is not None:
        collector.scene_signature = signature
    reference = store.load_reference(signature)
    if reference is not None:
        detector.accept(reference)
    seen = 0
    generation = camera.generation
    started_at = None
    settled_since = None
    previous = None
    process = psutil.Process()
    process.cpu_percent()
    last_health = time.monotonic()
    tray_state = None
    try:
        while not max_frames or seen < max_frames:
            tick = time.monotonic()
            if collector is not None:
                collector.poll()
            raw_frame = camera.read()
            frame = raw_frame
            interrupted = camera.generation != generation
            generation = camera.generation
            if activity_map is not None:
                if interrupted or (raw_frame is None and not getattr(camera, "healthy", False)):
                    activity_map.interrupt()
                if raw_frame is not None:
                    activity_map.update(raw_frame, dog_observer.detector.predict, tick)
            if frame is not None:
                seen += 1
                if tray_tracker is not None:
                    frame = tray_tracker.align(frame)
                    interrupted |= frame is None
            elif not getattr(camera, "healthy", False):
                interrupted = True
            if interrupted:
                machine.interrupt()
                started_at = None
                settled_since = None
                previous = None
                if stay is not None:
                    stay.reset()
                if dog_observer is not None:
                    dog_observer.reset()
                if outside_watch is not None:
                    outside_watch.reset()
            if tray_tracker is not None and (frame is not None or interrupted):
                state = "searching" if frame is None else "tracking"
                if state != tray_state:
                    tray_state = state
                    if on_tray_state is not None:
                        on_tray_state(state)
            if frame is None:
                time.sleep(1 / config.fps)
                continue
            valid_mask = roi.mask(frame.shape) if hasattr(roi, "mask") else None
            crop = roi.crop(frame)
            if stay is not None and stay.update(crop, valid_mask, tick):
                if notifier.send_early_alert():
                    logger.info("トイレ見守りの早期通知を送りました")
                if collector is not None:
                    collector.trigger(source="early_alert")
            observation = detector.analyze(crop, valid_mask)
            if dog_observer is not None:
                now = time.monotonic()
                dog_state = dog_observer.update(frame, roi, now)
                if outside_watch is not None:
                    boxes = [] if dog_state.error else dog_state.boxes
                    if (
                        outside_watch.update(boxes, frame.shape, now)
                        and collector is not None
                        and collector.trigger(source="outside_stay") is not None
                    ):
                        outside_watch.recorded = True
                if dog_state.error or dog_state.on_roi:
                    observation.kind = "occupied"
                transition = dog_state.transition
                if collector is not None and transition:
                    last = dog_clip_times.get(transition, float("-inf"))
                    if time.monotonic() - last >= config.dog_clip_cooldown_seconds:
                        if collector.trigger(source=transition) is not None:
                            dog_clip_times[transition] = time.monotonic()
            moving = previous is not None and detector.changed_from(crop, previous, valid_mask)
            previous = crop.copy()
            if moving:
                machine.interrupt()
                started_at = None
                settled_since = None
                if observation.kind == "candidate":
                    observation.kind = "unstable"
            if observation.kind == "candidate" and machine.started is None:
                started_at = utcnow()
            if observation.kind != "candidate":
                started_at = None
            if machine.update(observation.kind, time.monotonic()):
                event_id = store.record(
                    raw_frame,
                    observation.score,
                    started_at,
                    (time.monotonic() - tick) * 1000,
                    reference=crop,
                    signature=signature,
                )
                detector.accept(crop)
                machine = Machine(config.verify_seconds, config.clear_seconds)
                started_at = None
                settled_since = None
                store.set_monitor_state("SUSPECTED", event_id)
                if collector is not None:
                    collector.trigger(event_id)
                if not codex_notifications and notifier.send():
                    store.update(event_id, notified_at=utcnow())
                logger.info("排泄の疑いを記録しました: %s", event_id)
            if observation.kind == "empty":
                if settled_since is None:
                    settled_since = time.monotonic()
                if time.monotonic() - settled_since >= config.clear_seconds:
                    if detector.changed_from(crop, detector.reference_image, valid_mask):
                        store.save_reference(crop, signature)
                        detector.accept(crop)
            else:
                settled_since = None
            if time.monotonic() - last_health >= 30:
                logger.info(
                    "状態=%s CPU=%.1f%% RSS=%.1fMiB 処理=%.1fms",
                    machine.state,
                    process.cpu_percent(),
                    process.memory_info().rss / 1024**2,
                    (time.monotonic() - tick) * 1000,
                )
                last_health = time.monotonic()
                if dog_state is not None:
                    logger.info(
                        "犬検出ROI遮蔽=%s 推論=%.1fms", dog_state.on_roi, dog_state.inference_ms
                    )
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
        ("setup-activity-map", "行動マップ用にサークルの床の四隅を登録"),
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
    review = commands.add_parser("codex-review", help="保存済み動画をCodexで判定（通知なし）")
    review.add_argument("--limit", type=int, default=5, help="処理上限（1〜50件）")
    review.add_argument("--clip", help="判定する未ラベル動画のID")
    review.add_argument("--track-tray", action="store_true", help="動画内でトレーの位置に追従")
    review.add_argument("--detect-outside", action="store_true", help="登録した周辺範囲も判定")
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
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.load(args.config)
        if args.command == "run" and (args.dog or args.activity_map):
            config.dog_enabled = True
        root = Path(config.data_dir)
        tray_path = root / "baseline" / "tray-reference.png"
        outside_path = root / "outside-roi.json"
        outside_roi = None
        if getattr(args, "detect_outside", False):
            if args.command == "run" and not args.codex:
                print("トレー外検知には --codex が必要です", file=sys.stderr)
                return 2
            if not outside_path.is_file():
                print(
                    "先に setup-roi --outside でトレーと周囲の床を囲んでください", file=sys.stderr
                )
                return 2
            outside_roi = ROI.load(outside_path)
            config.dog_enabled = True
        if args.command == "codex-review" or (args.command == "run" and args.codex):
            from .clips import ClipStore
            from .codex_review import Review, inspect_clip, require_codex

            if args.command == "run" and not config.clip_enabled:
                print("--codexには動画保存を有効にしてください", file=sys.stderr)
                return 2
            try:
                require_codex()
            except ValueError as error:
                print(error, file=sys.stderr)
                return 2
            if args.command == "codex-review":
                if not 1 <= args.limit <= 50:
                    print("--limitは1〜50件で指定してください", file=sys.stderr)
                    return 2
                roi = ROI.load(root / "roi.json")
                baseline = load_baseline(root / "baseline" / "empty.png", roi)
                tray_tracker = (
                    TrayTracker.from_baseline(tray_path, baseline, roi) if args.track_tray else None
                )
                review = Review(
                    ClipStore(root),
                    lambda path, row: inspect_clip(
                        path, row, roi, tray_tracker=tray_tracker, outside_roi=outside_roi
                    ),
                    scene_signature=scene_signature(baseline, roi),
                )
                try:
                    completed, failed = review.run(args.limit, clip_id=args.clip)
                except ValueError as error:
                    print(error, file=sys.stderr)
                    return 2
                print(f"Codex判定候補 {completed}件、失敗 {failed}件")
                return 2 if failed else 0
        if args.command in ("setup-dog-model", "check-dog"):
            from .dog import DogDetector, setup_model

            model_dir = root / "models" / "dog-yolox"
            if args.command == "setup-dog-model":
                print(f"犬検出モデルを確認しました: {setup_model(model_dir)}")
                return 0
            if not 1 <= args.repeat <= 100:
                raise ValueError("繰り返し回数は1〜100です")
            frame = cv2.imread(args.image)
            if frame is None:
                raise ValueError("画像を読み込めません")
            model = DogDetector(model_dir, config.dog_confidence)
            model.predict(frame)
            process = psutil.Process()
            process.cpu_percent()
            start = time.perf_counter()
            for _ in range(args.repeat):
                boxes = model.predict(frame)
            print(
                json.dumps(
                    {
                        "dog_boxes": boxes,
                        "mean_inference_ms": (time.perf_counter() - start) * 1000 / args.repeat,
                        "cpu_percent_inference_loop": process.cpu_percent(),
                        "rss_mib": process.memory_info().rss / 1024**2,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "label-gui":
            from .label_gui import serve

            serve(root, args.port, not args.no_browser, disk_mb=config.clip_disk_mb)
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
        roi_path = (
            outside_path if args.command == "setup-roi" and args.outside else root / "roi.json"
        )
        baseline_path = root / "baseline" / "empty.png"
        if (
            args.command == "setup-activity-map"
            and (root / LAYOUT_FILE).exists()
            and not args.replace
        ):
            print(
                "床面は登録済みです。画角を変えた場合は --replace を指定してください",
                file=sys.stderr,
            )
            return 2
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
                tray_tracker = (
                    TrayTracker.from_baseline(tray_path, baseline, roi) if args.track_tray else None
                )
                activity = None
                if args.activity_map:
                    try:
                        layout = ActivityLayout.load(root)
                    except (OSError, ValueError, TypeError, KeyError):
                        print("先に setup-activity-map で床面を登録してください", file=sys.stderr)
                        return 2
                    activity = ActivityRecorder(ActivityStore(root), layout)
                with ExitStack() as stack:
                    if activity is not None:
                        stack.enter_context(activity.store.recording_lock())
                    collector = None
                    preview = LivePreview(root)
                    preview.set_tray_state("searching" if tray_tracker is not None else "fixed")
                    if config.clip_enabled:
                        from .clips import ClipCollector

                        collector = stack.enter_context(
                            ClipCollector(
                                root,
                                config.clip_before_seconds,
                                config.clip_after_seconds,
                                config.clip_fps,
                                config.clip_max_width,
                                config.clip_memory_mb,
                                config.clip_disk_mb,
                            )
                        )

                    def on_frame(frame, generation):
                        if collector is not None:
                            collector.feed(frame, generation=generation)
                        preview.publish(frame)

                    if args.codex:
                        review = Review(
                            collector.store,
                            lambda path, row: inspect_clip(
                                path,
                                row,
                                roi,
                                tray_tracker=tray_tracker,
                                outside_roi=outside_roi
                                if row["source"] in ("outside_stay", "manual")
                                else None,
                            ),
                            notifier.send_assessment if notifier.enabled else None,
                        )
                        stack.enter_context(review.watch())
                    live = stack.enter_context(
                        LatestCamera(
                            Camera(source, config.timeout_ms, config.retry_seconds), on_frame
                        )
                    )
                    live.wait_ready(config.timeout_ms / 1000 + 1)
                    monitor(
                        live,
                        roi,
                        baseline,
                        config,
                        notifier,
                        args.max_frames,
                        collector,
                        codex_notifications=args.codex,
                        early_alert=args.early_alert,
                        tray_tracker=tray_tracker,
                        on_tray_state=preview.set_tray_state,
                        outside_roi=outside_roi,
                        activity_map=activity,
                    )
                return 0
            frame = camera.read()
            if frame is None:
                raise ValueError("映像を取得できません。EufyのRTSP設定と接続を確認してください")
            if args.command == "check-camera":
                print(f"フレーム取得成功: {frame.shape[1]}x{frame.shape[0]}")
            elif args.command == "setup-roi":
                if args.outside:
                    roi = select_polygon(frame, "トレーと監視する周囲の床を指定")
                elif args.polygon:
                    roi = select_polygon(frame)
                else:
                    rect = cv2.selectROI("トイレ領域を選択してEnter、取消はEsc", frame)
                    cv2.destroyAllWindows()
                    roi = ROI.from_pixels(rect, frame.shape[1], frame.shape[0])
                roi.save(roi_path)
                print("ROIを保存しました")
            elif args.command == "setup-activity-map":
                floor = select_polygon(frame, "サークル床面の四隅を指定", floor=True)
                ActivityLayout(uuid4().hex, floor).save(root, frame)
                print("行動マップの床面を登録しました")
            elif args.command == "capture-baseline":
                roi = ROI.load(roi_path)
                if args.track_tray:
                    TrayTracker(frame, roi)
                    tray_path.parent.mkdir(parents=True, exist_ok=True)
                    if not cv2.imwrite(str(tray_path), frame):
                        raise OSError("トレー追従用の基準画像を保存できません")
                save_baseline(baseline_path, roi.crop(frame), roi)
                print("基準画像を保存しました")
        return 0
    except TrayReferenceError as error:
        print(error, file=sys.stderr)
        return 2
    except (ValueError, TypeError, OSError, cv2.error):
        # 外部例外の文字列に認証URLが含まれる可能性があるため出力しない。
        if not os.environ.get("POOP_RTSP_URL") and args.command in (
            "check-camera",
            "setup-roi",
            "setup-activity-map",
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
