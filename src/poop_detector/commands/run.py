"""監視の起動（poop-detector run）。"""

import argparse
from contextlib import ExitStack
from typing import TYPE_CHECKING

from ..activity_map import ActivityLayout, ActivityRecorder, ActivityStore
from ..camera import LatestCamera
from ..clips import ClipCollector
from ..config import load_connection_settings
from ..frames import Frame
from ..live_preview import LivePreview
from ..monitoring import monitor
from ..roi import AnyROI
from .context import (
    CommandContext,
    CommandError,
    build_notifier,
    camera_source,
    load_outside_roi,
    load_scene,
    load_tray_tracker,
    open_camera,
)
from .review import require_codex_login

if TYPE_CHECKING:
    from ..codex_review import Review
    from ..notifications import MacNotifier, NtfyNotifier
    from ..tray_tracking import TrayTracker


def run(args: argparse.Namespace, context: CommandContext) -> int:
    """引数の組み合わせを確かめてから、カメラ・動画収集・Codex 判定を準備して監視する。"""
    if args.dog or args.activity_map:
        context = context.with_dog_detection()
    outside_roi = None
    if args.detect_outside:
        if not args.codex:
            raise CommandError("トレー外検知には --codex が必要です")
        outside_roi = load_outside_roi(context)
        context = context.with_dog_detection()
    if args.codex:
        if not context.config.clip_enabled:
            raise CommandError("--codexには動画保存を有効にしてください")
        require_codex_login()
    settings = load_connection_settings()
    notifier, _ = build_notifier(context.config, settings, sends=True)
    source = camera_source(settings)
    roi, baseline = load_scene(context)
    tray_tracker = load_tray_tracker(context, baseline, roi, args.track_tray)
    activity = load_activity_recorder(context) if args.activity_map else None
    return watch(
        args, context, source, notifier, roi, baseline, tray_tracker, outside_roi, activity
    )


def load_activity_recorder(context: CommandContext) -> ActivityRecorder:
    try:
        layout = ActivityLayout.load(context.root)
    except (OSError, ValueError, TypeError, KeyError):
        raise CommandError("先に setup-activity-map で床面を登録してください") from None
    return ActivityRecorder(ActivityStore(context.root), layout)


def codex_watcher(
    collector: ClipCollector,
    roi: AnyROI,
    tray_tracker: "TrayTracker | None",
    outside_roi: AnyROI | None,
    notifier: "NtfyNotifier | MacNotifier",
) -> "Review":
    """監視中に保存した動画を Codex で判定し、根拠を確認できたときだけ通知する。"""
    from ..codex_review import Review, inspect_clip

    return Review(
        collector.store,
        lambda path, row: inspect_clip(
            path,
            row,
            roi,
            tray_tracker=tray_tracker,
            # 周辺範囲はトレー外での滞在と手動保存の動画だけで判定する。
            outside_roi=outside_roi if row["source"] in ("outside_stay", "manual") else None,
        ),
        notifier.send_assessment if notifier.enabled else None,
    )


def watch(
    args: argparse.Namespace,
    context: CommandContext,
    source: str,
    notifier: "NtfyNotifier | MacNotifier",
    roi: AnyROI,
    baseline: Frame,
    tray_tracker: "TrayTracker | None",
    outside_roi: AnyROI | None,
    activity: ActivityRecorder | None,
) -> int:
    """監視に必要なものを開き、終わったら逆順に閉じる。"""
    config = context.config
    with ExitStack() as stack:
        if activity is not None:
            stack.enter_context(activity.store.recording_lock())
        collector = None
        preview = LivePreview(context.root)
        preview.set_tray_state("searching" if tray_tracker is not None else "fixed")
        if config.clip_enabled:
            collector = stack.enter_context(
                ClipCollector(
                    context.root,
                    config.clip_before_seconds,
                    config.clip_after_seconds,
                    config.clip_fps,
                    config.clip_max_width,
                    config.clip_memory_mb,
                    config.clip_disk_mb,
                )
            )

        def on_frame(frame: Frame | None, generation: int) -> None:
            if collector is not None:
                collector.feed(frame, generation=generation)
            preview.publish(frame)

        if args.codex:
            if collector is None:
                raise CommandError("--codexには動画保存を有効にしてください")
            review = codex_watcher(collector, roi, tray_tracker, outside_roi, notifier)
            stack.enter_context(review.watch())
        live = stack.enter_context(LatestCamera(open_camera(source, config), on_frame))
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
