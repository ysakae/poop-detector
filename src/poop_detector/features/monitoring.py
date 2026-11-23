"""設定に応じて監視の拡張を組み立て、検知ループを起動する。"""

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..config import Config
from ..detection.monitoring import (
    Clock,
    DetectionNotifier,
    FrameSource,
    FrameView,
    Monitor,
    MonitorExtension,
    SystemClock,
)
from ..detection.roi import ROI, AnyROI
from ..detection.scene import scene_signature
from ..frames import Frame
from ..records.database import events_database
from ..records.events import utcnow
from ..records.sheet_wear import SheetWearLog
from ..video.recording import ClipCollector
from .activity_map import ActivityRecorder
from .early_alert import EarlyAlert
from .sheet_wear import SheetWearExtension
from .sleep import SleepRecorder
from .tray_tracking import TrayTracker

if TYPE_CHECKING:
    from ..experimental.outside import OutsideWatch
    from ..notifications import Delivery
    from .dog import Box, DogObserver, DogState, DogTransition, Predict

logger = logging.getLogger(__name__)


class MonitorNotifier(DetectionNotifier, Protocol):
    """標準の監視拡張を含めた通知先。"""

    def send_early_alert(
        self, *, clip_id: str | None = None, detected_at: str | None = None
    ) -> bool: ...
    def send_reentry(
        self, kind: str, event_id: str, at: datetime, clip_id: str | None
    ) -> "Delivery": ...
    def send_sheet_due(self, events: int, ratio: float) -> bool: ...
    def send_floor_object(self, object_id: str, detected_at: str) -> bool: ...
    def send_absence(self, since: datetime, minutes: float) -> bool: ...
    def send_return(self, minutes: float) -> bool: ...


class EarlyAlertExtension(MonitorExtension):
    """録画を待たずに、トイレの同じ場所での滞在を知らせる（--early-alert）。"""

    def __init__(
        self, stay: EarlyAlert, notifier: MonitorNotifier, collector: ClipCollector | None
    ) -> None:
        self.stay, self.notifier, self.collector = stay, notifier, collector

    def on_interrupt(self) -> None:
        self.stay.reset()

    def on_frame(self, view: FrameView) -> None:
        if not self.stay.update(view.crop, view.valid_mask, view.tick):
            return
        detected_at = utcnow()
        clip_id = self.collector.trigger(source="early_alert") if self.collector else None
        if self.notifier.send_early_alert(clip_id=clip_id, detected_at=detected_at):
            logger.info("トイレ見守りの早期通知を送りました")


# 犬がトレーに重なっている間と、離れてからこの秒数の間は、トレー外の滞在として数えない。
# トレーへの出入りの途中で立ち止まった場面（犬の入退場の動画に映る）を、別に録画しないため。
TRAY_GRACE_SECONDS = 10.0


class DogExtension(MonitorExtension):
    """犬が ROI に重なる間は判定を保留し、入退場とトレー外の滞在で動画を保存する。"""

    def __init__(
        self,
        observer: "DogObserver",
        roi: AnyROI,
        config: Config,
        clock: Clock,
        collector: ClipCollector | None,
        outside_watch: "OutsideWatch | None" = None,
    ) -> None:
        self.observer, self.roi, self.config, self.clock = observer, roi, config, clock
        self.collector, self.outside_watch = collector, outside_watch
        self.state: DogState | None = None
        self.clip_times: dict[DogTransition, float] = {}
        # トレー外の滞在を最後に録画した時刻と、前の録画と重ねないよう先に延ばした録画の時刻。
        self.outside_clip_at = float("-inf")
        self.outside_due: float | None = None
        # 犬がトレーに重なっていた最後の時刻。
        self.tray_seen_at = float("-inf")

    def on_interrupt(self) -> None:
        self.observer.reset()
        self.outside_due = None
        self.tray_seen_at = float("-inf")
        if self.outside_watch is not None:
            self.outside_watch.reset()

    def on_frame(self, view: FrameView) -> None:
        now = self.clock.monotonic()
        state = self.state = self.observer.update(view.frame, self.roi, now)
        if state.on_roi:
            self.tray_seen_at = now
        if self.outside_watch is not None:
            near_tray = now - self.tray_seen_at < TRAY_GRACE_SECONDS
            boxes = [] if state.error or near_tray else state.boxes
            self._record_outside(self.outside_watch.update(boxes, view.frame.shape, now), now)
        # 推論に失敗した間も、犬がいる可能性があるので判定を保留する。
        if state.error or state.on_roi:
            view.observation.kind = "occupied"
        self._record_transition(state)

    def _record_outside(self, staying: bool, now: float) -> None:
        """トレー外の滞在を録画する。前の録画と時間が重なるときは、重ならない時刻まで延ばす。

        犬が少し動くたびに新しい滞在になるので、そのたびに録画すると同じ場面の動画が
        何本もでき、判定器を何度も呼ぶことになる。延ばした録画は前の録画の直後から始まるので、
        滞在の間の映像は途切れない。延ばしている間の滞在は、その1本にまとめる。
        """
        if self.collector is None or self.outside_watch is None:
            return
        if staying:
            self.outside_watch.recorded = True
            if self.outside_due is None:
                spacing = self.config.clip_before_seconds + self.config.clip_after_seconds
                self.outside_due = max(now, self.outside_clip_at + spacing)
        # 同時処理数を超えて録画できなかったときは、次のフレームでやり直す。
        if (
            self.outside_due is not None
            and now >= self.outside_due
            and self.collector.trigger(source="outside_stay") is not None
        ):
            self.outside_clip_at = now
            self.outside_due = None

    def _record_transition(self, state: "DogState") -> None:
        """入場・退場の前後を、種類ごとに dog_clip_cooldown_seconds の間隔で録画する。"""
        transition = state.transition
        if self.collector is None or not transition:
            return
        last = self.clip_times.get(transition, float("-inf"))
        if self.clock.monotonic() - last < self.config.dog_clip_cooldown_seconds:
            return
        stay = state.tray_stay_seconds
        clip_id = (
            self.collector.trigger(source=transition)
            if stay is None
            else self.collector.trigger(source=transition, tray_stay_seconds=stay)
        )
        if clip_id is not None:
            self.clip_times[transition] = self.clock.monotonic()

    def log_status(self) -> None:
        if self.state is not None:
            logger.info("犬検出ROI遮蔽=%s 推論=%.1fms", self.state.on_roi, self.state.inference_ms)


class ActivityExtension(MonitorExtension):
    """位置合わせ前の映像で犬の位置を求め、行動マップに記録する（--activity-map）。"""

    def __init__(self, recorder: ActivityRecorder, predict: "Predict") -> None:
        self.recorder, self.predict = recorder, predict

    def on_camera_frame(self, raw: Frame | None, disconnected: bool, tick: float) -> None:
        # トレー追従に失敗しても床面の映像は使えるので、接続の途切れだけで区切る。
        if disconnected:
            self.recorder.interrupt()
        if raw is not None:
            self.recorder.update(raw, self.predict, tick)


class SharedPredict:
    """犬検出の結果を、同じフレームの間だけ使い回す。

    行動マップと睡眠の記録が同じ映像を続けて処理するとき、推論を1回で済ませる。
    直前に渡されたフレームのオブジェクトと同じ（is）ときだけ前の結果を返す。
    """

    def __init__(self, predict: "Predict") -> None:
        self.predict = predict
        self._frame: Frame | None = None
        self._boxes: list[Box] = []

    def __call__(self, frame: Frame) -> list["Box"]:
        if frame is not self._frame:
            # 推論が失敗したときに、前のフレームの結果を残さない。
            self._frame = None
            self._boxes = self.predict(frame)
            self._frame = frame
        return list(self._boxes)


class SleepExtension(MonitorExtension):
    """位置合わせ前の映像で犬の在室と静止を観測し、睡眠を記録する（--sleep）。"""

    def __init__(self, recorder: SleepRecorder, predict: "Predict") -> None:
        self.recorder, self.predict = recorder, predict

    def on_camera_frame(self, raw: Frame | None, disconnected: bool, tick: float) -> None:
        # トレー追従に失敗しても床面の映像は使えるので、接続の途切れだけで区切る。
        if disconnected:
            self.recorder.interrupt()
        if raw is not None:
            self.recorder.update(raw, self.predict, tick)


def dog_observer_from_config(config: Config) -> "DogObserver":
    """設定の犬検出モデルを読み込む。モデルがなければ準備の方法をログに出して送出する。"""
    from .dog import DogDetector, DogObserver

    try:
        model = DogDetector(Path(config.data_dir) / "models" / "dog-yolox", config.dog_confidence)
    except (OSError, ValueError):
        logger.error("犬のモデルを準備してください: uv run poop-detector setup-dog-model")
        raise
    return DogObserver(model, config.dog_fps, config.dog_exit_seconds)


def _floor_extension(
    floor: AnyROI, tray: AnyROI, config: Config, notifier: MonitorNotifier, predict: "Predict"
) -> MonitorExtension:
    from ..experimental.floor_objects import FloorObjectExtension, FloorObjectWatch
    from ..records.floor_objects import FloorObjects

    objects = FloorObjects(events_database(config.data_dir), config.data_dir)
    watch = FloorObjectWatch(floor, tray)
    return FloorObjectExtension(watch, predict, objects, notifier, notify=config.floor_notify)


def _presence_extension(
    floor: AnyROI, config: Config, notifier: MonitorNotifier, predict: "Predict"
) -> MonitorExtension:
    import numpy as np

    from ..records.absences import Absences, AwayMode
    from .presence import PresenceExtension, PresenceTracker
    from .sleep_motion import MotionSampler

    database = events_database(config.data_dir)
    tracker = PresenceTracker(Absences(database), AwayMode(database), config, notifier)
    # 在室の観測は、睡眠の記録と同じく床面の中だけで犬の動きを見る（正規化した頂点）。
    if isinstance(floor, ROI):
        x, y, w, h = floor.x, floor.y, floor.width, floor.height
        points = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
    else:
        points = floor.points
    sampler = MotionSampler(np.array(points, dtype=np.float32))
    return PresenceExtension(tracker, sampler, predict)


def _shared_frame_extensions(
    roi: AnyROI,
    config: Config,
    notifier: MonitorNotifier,
    dog_observer: "DogObserver | None",
    *,
    activity_map: ActivityRecorder | None,
    sleep: SleepRecorder | None,
    floor: AnyROI | None,
) -> list[MonitorExtension]:
    """位置合わせ前の映像を使う拡張で、同じコマの犬検出を共有する。"""
    if activity_map is None and sleep is None and floor is None:
        return []
    extensions: list[MonitorExtension] = []
    if dog_observer is None:
        raise ValueError("行動マップ・睡眠の記録・床の候補には犬の検出が必要です")
    # 同じ映像を処理する拡張の間で、犬検出の推論を共有する。
    predict = SharedPredict(dog_observer.predict)
    # 位置合わせ前の映像を使うので、カメラの読み込み直後に呼ばれる。
    if activity_map is not None:
        extensions.append(ActivityExtension(activity_map, predict))
    if sleep is not None:
        extensions.append(SleepExtension(sleep, predict))
    if floor is not None and config.floor_objects_enabled:
        extensions.append(_floor_extension(floor, roi, config, notifier, predict))
    if floor is not None and config.presence_enabled:
        extensions.append(_presence_extension(floor, config, notifier, predict))
    return extensions


def standard_extensions(
    roi: AnyROI,
    baseline: Frame,
    config: Config,
    notifier: MonitorNotifier,
    clock: Clock,
    *,
    collector: ClipCollector | None = None,
    dog_observer: "DogObserver | None" = None,
    early_alert: bool = False,
    outside_roi: AnyROI | None = None,
    activity_map: ActivityRecorder | None = None,
    sleep: SleepRecorder | None = None,
    floor: AnyROI | None = None,
) -> list[MonitorExtension]:
    """CLI の --early-alert・--dog・--detect-outside・--activity-map・--sleep・--floor-objects・
    --presence に対応する拡張を作る。floor は床の候補と不在の記録に使う床面（行動マップの登録）。
    """
    if dog_observer is None and config.dog_enabled:
        dog_observer = dog_observer_from_config(config)
    extensions = _shared_frame_extensions(
        roi, config, notifier, dog_observer, activity_map=activity_map, sleep=sleep, floor=floor
    )
    if config.digest_enabled or config.night_enabled:
        from ..records.away_periods import AwayPeriods
        from .digest import DigestSnapshots, SnapshotExtension

        periods = AwayPeriods(events_database(config.data_dir))
        snapshots = DigestSnapshots(config.data_dir)
        night = config.night_hours if config.night_enabled else None
        extensions.append(
            SnapshotExtension(periods, snapshots, config.digest_snapshot_seconds, night_hours=night)
        )
    if config.sheet_enabled:
        log = SheetWearLog(events_database(config.data_dir))
        signature = scene_signature(baseline, roi)
        extensions.append(SheetWearExtension(log, signature, config, notifier))
    if early_alert:
        extensions.append(EarlyAlertExtension(EarlyAlert(baseline, config), notifier, collector))
    if dog_observer is not None:
        watch = None
        if outside_roi is not None:
            from ..experimental.outside import OutsideWatch

            watch = OutsideWatch(roi, outside_roi, config.dog_exit_seconds)
        dog = DogExtension(dog_observer, roi, config, clock, collector, watch)
        extensions.append(dog)
        if config.reentry_enabled:
            from .reentry_extension import ReentryExtension

            if collector is None:
                raise ValueError("再侵入検知には動画保存が必要です")
            extensions.append(ReentryExtension(config, dog, notifier, collector, baseline))
    return extensions


def monitor(
    camera: FrameSource,
    roi: AnyROI,
    baseline: Frame,
    config: Config,
    notifier: MonitorNotifier,
    max_frames: int = 0,
    collector: ClipCollector | None = None,
    dog_observer: "DogObserver | None" = None,
    review_notifications: bool = False,
    early_alert: bool = False,
    tray_tracker: TrayTracker | None = None,
    on_tray_state: Callable[[str], None] | None = None,
    outside_roi: AnyROI | None = None,
    activity_map: ActivityRecorder | None = None,
    sleep: SleepRecorder | None = None,
    *,
    extensions: Sequence[MonitorExtension] = (),
    clock: Clock | None = None,
    before_align: Callable[[Monitor, Frame | None], bool] | None = None,
) -> int:
    """監視ループを動かし、処理したフレーム数を返す。max_frames が 0 なら止めるまで動く。

    早期通知・犬の検出・トレー外・行動マップは、引数から拡張を組み立てて差し込む。
    新しい機能は extensions に渡せば、この関数と Monitor を変えずに足せる。
    """
    clock = clock or SystemClock()
    built = standard_extensions(
        roi,
        baseline,
        config,
        notifier,
        clock,
        collector=collector,
        dog_observer=dog_observer,
        early_alert=early_alert,
        outside_roi=outside_roi,
        activity_map=activity_map,
        sleep=sleep,
    )
    return Monitor(
        camera,
        roi,
        baseline,
        config,
        notifier,
        collector=collector,
        review_notifications=review_notifications,
        tray_tracker=tray_tracker,
        on_tray_state=on_tray_state,
        extensions=[*built, *extensions],
        clock=clock,
        before_align=before_align,
    ).run(max_frames)
