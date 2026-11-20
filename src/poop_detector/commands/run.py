"""監視の起動（poop-detector run）。"""

import argparse
import logging
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..capture.camera import LatestCamera
from ..capture.live_preview import LivePreview
from ..config import Config, check_monitor_features, load_connection_settings, with_flags
from ..detection.monitoring import Monitor, MonitorExtension, SystemClock
from ..detection.roi import AnyROI
from ..detection.scene import scene_signature
from ..features.activity_map import ActivityLayout, ActivityRecorder, ActivityStore
from ..features.monitoring import (
    dog_observer_from_config,
    standard_extensions,
)
from ..features.sleep import SleepRecorder, sleep_recorder
from ..features.tray_registration import TrayRegistration, TrayScene
from ..frames import Frame
from ..gui.server import background_gui
from ..records.database import events_database
from ..video.recording import ClipCollector
from ..web_address import WebOptions
from .context import (
    CommandContext,
    CommandError,
    build_notifier,
    camera_source,
    load_outside_roi,
    load_tray_tracker,
    monitor_watchdog,
    open_camera,
    outage_tracker,
    with_web_push,
)
from .review import ready_judge

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..features.tray_tracking import TrayTracker
    from ..features.watchdog import JudgeWatchdog
    from ..notifications import ToiletNotifier
    from ..video.clip_reviews import Assessment
    from ..video.clips import ClipRow
    from ..video.judge_source import JudgeSource
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
        sleep=args.sleep,
    )
    context = replace(context, config=check_monitor_features(config))
    config = context.config
    outside_roi = load_outside_roi(context) if config.detect_outside_enabled else None
    judge = ready_judge(config) if config.video_review_enabled else None
    settings = load_connection_settings()
    primary, _ = build_notifier(context.config, settings, sends=True)
    # ntfy・macOS の通知に加えて、GUI で購読した人の端末へ Web Push でも送る。
    notifier = with_web_push(primary, context)
    source = camera_source(settings)
    roi, baseline = context.scene.load()
    tray_tracker = load_tray_tracker(context.scene, baseline, roi, config.track_tray_enabled)
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


def judge_source(config_path: str, config: Config, judge: "VideoJudge") -> "JudgeSource":
    """管理 › 判定器とモデルで選び直した判定器を、監視を止めずに次の動画から使う。"""
    from ..video.judge_source import FallbackJudges, JudgeChoice, ReloadingJudge

    initial = JudgeChoice(judge, config.review_provider, config.review_model or None)
    primary = ReloadingJudge(config_path, initial)
    spares = config.fallback_providers
    return FallbackJudges(primary, spares) if spares else primary


def review_watcher(
    collector: ClipCollector,
    initial: TrayScene,
    scenes: dict[str, TrayScene],
    outside_roi: AnyROI | None,
    notifier: "ToiletNotifier",
    judges: "JudgeSource",
    max_per_hour: int = 0,
    screening: tuple[Path, float] | None = None,
) -> "Review":
    """録画時の基準で動画を自動判定する。判定器は動画ごとに judges から受け取る。

    screening は（犬検出モデルの場所, 検出のしきい値）。あれば、判定の前にふるい分けの
    指標を記録する（判定は省かない）。
    """
    from ..video.review import Review, inspect_clip

    def scene_of(row: "ClipRow") -> TrayScene:
        signature = row["scene_signature"]
        return scenes.get(signature, initial) if signature is not None else initial

    def inspect(path: Path, row: "ClipRow", judge: "VideoJudge") -> "Assessment":
        scene = scene_of(row)
        return inspect_clip(
            path,
            row,
            scene.roi,
            judge,
            tray_tracker=scene.tracker,
            # 周辺範囲はトレー外での滞在と手動保存の動画だけで判定する。
            outside_roi=outside_roi if row["source"] in ("outside_stay", "manual") else None,
        )

    screen = None
    if screening is not None:
        from ..experimental.screening import ClipScreener

        screen = ClipScreener(
            collector.store.screenings,
            lambda row: (scene_of(row).roi, scene_of(row).tracker),
            *screening,
        )
    return Review(
        collector.store,
        inspect,
        notifier.send_assessment if notifier.enabled else None,
        judges=judges,
        max_per_hour=max_per_hour,
        screen=screen,
    )


_EXCHANGE_PHASES = ("begin", "paused", "apply", "failed", "cancel")


@dataclass
class _TrayExchange:
    """監視のフレーム境界で、新しいシーンを反映してから録画を再開する。"""

    registration: TrayRegistration
    scenes: dict[str, TrayScene]
    scene_extensions: Callable[[TrayScene], list[MonitorExtension]]
    collector: ClipCollector | None
    track_tray: bool
    sleep: SleepRecorder | None = None

    def before_align(self, engine: Monitor, frame: Frame | None) -> bool:
        if self.registration.status()["phase"] not in _EXCHANGE_PHASES:
            return False
        if self.collector is not None:
            self.collector.set_maintenance(True)
        scene = self.registration.poll(frame, engine.roi, track_tray=self.track_tray)
        if scene is not None:
            self.scenes[scene_signature(scene.baseline, scene.roi)] = scene
            engine.replace_scene(
                scene.roi, scene.baseline, scene.tracker, self.scene_extensions(scene)
            )
            if self.sleep is not None:
                # 交換中は拡張にフレームが届かず、再開後の差分が古い映像と比べられる。
                self.sleep.interrupt()
        paused = self.registration.status()["phase"] in _EXCHANGE_PHASES
        if self.collector is not None:
            self.collector.set_maintenance(paused)
        return paused


def watch(
    args: argparse.Namespace,
    context: CommandContext,
    source: str,
    notifier: "ToiletNotifier",
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
        registration = TrayRegistration(context.root)
        initial = TrayScene(roi, baseline, tray_tracker)
        scenes = {scene_signature(baseline, roi): initial}
        dog_observer = dog_observer_from_config(config) if config.dog_enabled else None
        sleep = (
            sleep_recorder(events_database(context.root), context.root, config)
            if config.sleep_enabled
            else None
        )
        if sleep is not None:
            # 異常終了でも、判定中の分を書いて閉じる。
            stack.callback(sleep.close)
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
                    auto_cleanup=config.clip_auto_cleanup,
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
                initial,
                scenes,
                outside_roi,
                notifier,
                judge_source(args.config, config, judge),
                config.review_max_per_hour,
                # 犬検出を使うときだけ、判定の前にふるい分けの指標を記録する。
                (context.dog_model_dir, config.dog_confidence) if config.dog_enabled else None,
            )
            stack.enter_context(review.watch(judge_watchdog(context, notifier)))
        if args.gui:
            # 同じプロセスの GUI からも、監視ループが止まっていないかを見張る。
            stack.enter_context(monitor_watchdog(context, notifier))
            web = WebOptions.from_config(config, port=args.gui_port)
            stack.enter_context(
                background_gui(
                    context.root,
                    web.port,
                    not args.no_browser,
                    disk_mb=config.clip_disk_mb,
                    pet_name=config.pet_name,
                    web=web,
                    config_path=args.config,
                    review_enabled=config.video_review_enabled,
                )
            )
        watchdog = camera_watchdog(context, notifier)
        live = stack.enter_context(LatestCamera(open_camera(source, config), on_frame))
        live.wait_ready(config.timeout_ms / 1000 + 1)

        clock = SystemClock()

        def scene_extensions(scene: TrayScene) -> list[MonitorExtension]:
            extensions = standard_extensions(
                scene.roi,
                scene.baseline,
                config,
                notifier,
                clock,
                collector=collector,
                dog_observer=dog_observer,
                early_alert=config.early_alert_enabled,
                outside_roi=outside_roi,
                activity_map=activity,
                sleep=sleep,
            )
            return [*extensions, *watchdog]

        exchange = _TrayExchange(
            registration, scenes, scene_extensions, collector, config.track_tray_enabled, sleep
        )

        def before_align(engine: Monitor, frame: Frame | None) -> bool:
            preview.pulse(engine.camera.healthy)
            return exchange.before_align(engine, frame)

        Monitor(
            live,
            roi,
            baseline,
            config,
            notifier,
            collector=collector,
            before_align=before_align,
            review_notifications=config.video_review_enabled,
            tray_tracker=tray_tracker,
            on_tray_state=preview.set_tray_state,
            extensions=scene_extensions(initial),
            clock=clock,
        ).run(args.max_frames)
        # 自分で終了したことを残し、GUI の見張りが停止として知らせないようにする。
        preview.mark_stopped()
    return 0


def judge_watchdog(context: CommandContext, notifier: "ToiletNotifier") -> "JudgeWatchdog | None":
    """判定器の呼び出しが watchdog.minutes 失敗し続けたら知らせる見張り（無効なら None）。"""
    if not context.config.watchdog_enabled:
        return None
    from ..features.watchdog import JudgeWatchdog

    return JudgeWatchdog(outage_tracker(context, "judge", notifier))


def camera_watchdog(context: CommandContext, notifier: "ToiletNotifier") -> list[MonitorExtension]:
    """カメラの映像が watchdog.minutes 届かなければ知らせる拡張（無効なら空）。"""
    if not context.config.watchdog_enabled:
        return []
    from ..features.watchdog import CameraWatchdogExtension

    return [CameraWatchdogExtension(outage_tracker(context, "camera", notifier))]
