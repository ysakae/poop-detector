"""カメラのフレームを順に判定し、記録・通知・動画保存へつなぐ監視ループ。"""

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import psutil

from .activity_map import ActivityRecorder
from .clips import ClipCollector
from .config import Config
from .detection import IncrementalDetector
from .early_alert import EarlyAlert
from .events import Store, utcnow
from .frames import Frame
from .roi import AnyROI
from .scene import scene_signature
from .state_machine import Machine
from .tray_tracking import TrayTracker

if TYPE_CHECKING:
    from .dog import DogObserver, DogState, DogTransition

logger = logging.getLogger(__name__)


class FrameSource(Protocol):
    """監視ループが読むカメラ。generation は再接続のたびに増える。

    healthy 属性があれば、フレームが届かないときに接続の途切れと区別するのに使う。
    """

    generation: int

    def read(self) -> Frame | None: ...


class MonitorNotifier(Protocol):
    """監視ループが使う通知。"""

    def send(self) -> bool: ...
    def send_early_alert(self) -> bool: ...


def monitor(
    camera: FrameSource,
    roi: AnyROI,
    baseline: Frame,
    config: Config,
    notifier: MonitorNotifier,
    max_frames: int = 0,
    collector: ClipCollector | None = None,
    dog_observer: "DogObserver | None" = None,
    codex_notifications: bool = False,
    early_alert: bool = False,
    tray_tracker: TrayTracker | None = None,
    on_tray_state: Callable[[str], None] | None = None,
    outside_roi: AnyROI | None = None,
    activity_map: ActivityRecorder | None = None,
) -> int:
    """監視ループ。max_frames が 0 なら止めるまで動き、処理したフレーム数を返す。"""
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
    if activity_map is not None and dog_observer is None:
        raise ValueError("行動マップには犬の検出が必要です")
    dog_state: DogState | None = None
    dog_clip_times: dict[DogTransition, float] = {}
    signature = scene_signature(baseline, roi)
    if collector is not None:
        collector.scene_signature = signature
    reference = store.load_reference(signature)
    if reference is not None:
        detector.accept(reference)
    seen = 0
    generation = camera.generation
    started_at: str | None = None
    settled_since: float | None = None
    previous: Frame | None = None
    process = psutil.Process()
    process.cpu_percent()
    last_health = time.monotonic()
    tray_state: str | None = None
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
                if raw_frame is not None and dog_observer is not None:
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
                    if (
                        time.monotonic() - last >= config.dog_clip_cooldown_seconds
                        and collector.trigger(source=transition) is not None
                    ):
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
                # ここに来るのは frame があるときだけで、frame は raw_frame から作る。
                event_id = store.record(
                    cast(Frame, raw_frame),
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
                if time.monotonic() - settled_since >= config.clear_seconds and (
                    detector.changed_from(crop, detector.reference_image, valid_mask)
                ):
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
