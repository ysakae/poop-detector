"""監視の起動（poop-detector run）。"""

import argparse
import logging
from contextlib import ExitStack
from dataclasses import replace
from typing import TYPE_CHECKING

from ..capture.camera import LatestCamera
from ..capture.live_preview import LivePreview
from ..config import check_monitor_features, load_connection_settings, with_flags
from ..detection.monitoring import monitor
from ..detection.roi import AnyROI
from ..features.activity_map import ActivityLayout, ActivityRecorder, ActivityStore
from ..frames import Frame
from ..video.recording import ClipCollector
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
from .review import ready_judge

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..features.tray_tracking import TrayTracker
    from ..notifications import MacNotifier, NtfyNotifier
    from ..video.review import Review, VideoJudge


def run(args: argparse.Namespace, context: CommandContext) -> int:
    """引数の組み合わせを確かめてから、カメラ・動画収集・自動判定を準備して監視する。"""
    config = with_flags(
        context.config,
        video_review=args.video_review,
        early_alert=args.early_alert,
        track_tray=args.track_tray,
        activity_map=args.activity_map,
        detect_outside=args.detect_outside,
        dog=args.dog,
    )
    context = replace(context, config=check_monitor_features(config))
    config = context.config
    outside_roi = load_outside_roi(context) if config.detect_outside_enabled else None
    judge = ready_judge(config) if config.video_review_enabled else None
    settings = load_connection_settings()
    notifier, _ = build_notifier(context.config, settings, sends=True)
    source = camera_source(settings)
    roi, baseline = load_scene(context)
    tray_tracker = load_tray_tracker(context, baseline, roi, config.track_tray_enabled)
    activity = load_activity_recorder(context) if config.activity_map_enabled else None
    logger.info("有効な機能: %s", "、".join(config.enabled_features()) or "なし")
    return watch(
        args, context, source, notifier, roi, baseline, tray_tracker, outside_roi, activity, judge
    )


def load_activity_recorder(context: CommandContext) -> ActivityRecorder:
    try:
        layout = ActivityLayout.load(context.root)
    except (OSError, ValueError, TypeError, KeyError):
        raise CommandError("先に setup-activity-map で床面を登録してください") from None
    return ActivityRecorder(ActivityStore(context.root), layout)


def review_watcher(
    collector: ClipCollector,
    roi: AnyROI,
    tray_tracker: "TrayTracker | None",
    outside_roi: AnyROI | None,
    notifier: "NtfyNotifier | MacNotifier",
    judge: "VideoJudge",
    *,
    provider: str,
    model: str | None,
) -> "Review":
    """監視中に保存した動画を自動判定し、根拠を確認できたときだけ通知する。"""
    from ..video.review import Review, inspect_clip

    return Review(
        collector.store,
        lambda path, row: inspect_clip(
            path,
            row,
            roi,
            judge,
            tray_tracker=tray_tracker,
            # 周辺範囲はトレー外での滞在と手動保存の動画だけで判定する。
            outside_roi=outside_roi if row["source"] in ("outside_stay", "manual") else None,
        ),
        notifier.send_assessment if notifier.enabled else None,
        provider=provider,
        model=model,
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
    judge: "VideoJudge | None" = None,
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

        if config.video_review_enabled and judge is not None:
            if collector is None:
                raise CommandError(
                    "動画の自動判定には動画保存（clips.enabled）を有効にしてください"
                )
            review = review_watcher(
                collector,
                roi,
                tray_tracker,
                outside_roi,
                notifier,
                judge,
                provider=config.review_provider,
                model=config.review_model or None,
            )
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
            review_notifications=config.video_review_enabled,
            early_alert=config.early_alert_enabled,
            tray_tracker=tray_tracker,
            on_tray_state=preview.set_tray_state,
            outside_roi=outside_roi,
            activity_map=activity,
        )
    return 0
