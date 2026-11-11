"""カメラのフレームを順に判定し、記録・通知・動画保存へつなぐ監視ループ。

1フレームの処理は次の段に分かれる。

1. 読み込み: カメラからフレームを読み、再接続や接続の途切れを中断として扱う。
2. 位置合わせ: トレー追従で登録時の座標へ戻す。合わせられなければ中断。
3. 観測: ROI で切り出して差分を判定し、拡張（早期通知、犬の検出など）が観測を上書きする。
4. 状態遷移: フレーム間の動きを確かめ、候補が続いたら記録・通知・動画保存へ進む。
5. 通知済み状態の更新: 清掃などで空が続いたら、通知済みの状態を空に戻す。

任意の機能は MonitorExtension として差し込み、Monitor の本体を変えずに足せる。
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import psutil

from ..config import Config
from ..features.activity_map import ActivityRecorder
from ..features.early_alert import EarlyAlert
from ..features.tray_tracking import TrayTracker
from ..frames import Frame, Mask
from ..records.events import Store, utcnow
from ..video.recording import ClipCollector
from .difference import IncrementalDetector, Observation
from .roi import AnyROI
from .scene import scene_signature
from .state_machine import Machine

if TYPE_CHECKING:
    from ..experimental.outside import OutsideWatch
    from ..features.dog import DogObserver, DogState, DogTransition, Predict

logger = logging.getLogger(__name__)
HEALTH_LOG_SECONDS = 30


class FrameSource(Protocol):
    """監視ループが読むカメラ。

    generation は再接続のたびに増える。healthy は、新しいフレームが届かないときに、
    単に待っているだけか（True）、接続が途切れているか（False）を区別するのに使う。
    """

    @property
    def generation(self) -> int: ...

    @property
    def healthy(self) -> bool: ...

    def read(self) -> Frame | None: ...


class MonitorNotifier(Protocol):
    """監視ループが使う通知。"""

    def send(self) -> bool: ...
    def send_early_alert(self) -> bool: ...


class Clock(Protocol):
    """監視ループの時計。テストでは時刻を進める偽の時計に差し替える。"""

    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """time モジュールの単調時計と sleep。"""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class FrameView:
    """位置合わせ後の1フレームと、その観測。拡張は observation を上書きできる。"""

    raw: Frame
    frame: Frame
    crop: Frame
    valid_mask: Mask | None
    observation: Observation
    tick: float


class MonitorExtension:
    """監視ループに差し込む任意の機能。必要な呼び出し口だけを上書きする。"""

    def on_camera_frame(self, raw: Frame | None, disconnected: bool, tick: float) -> None:
        """位置合わせの前に、カメラから読んだままのフレームを受け取る。

        disconnected は再接続か接続の途切れ。トレー追従の失敗は含まない。
        """

    def on_interrupt(self) -> None:
        """再接続・接続の途切れ・追従の失敗で、続いていた判定を捨てる。"""

    def on_frame(self, view: FrameView) -> None:
        """位置合わせと差分の判定を終えたフレームを受け取る。"""

    def log_status(self) -> None:
        """稼働ログ（HEALTH_LOG_SECONDS ごと）に、機能ごとの状態を書く。"""


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
        if self.notifier.send_early_alert():
            logger.info("トイレ見守りの早期通知を送りました")
        if self.collector is not None:
            self.collector.trigger(source="early_alert")


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

    def on_interrupt(self) -> None:
        self.observer.reset()
        if self.outside_watch is not None:
            self.outside_watch.reset()

    def on_frame(self, view: FrameView) -> None:
        now = self.clock.monotonic()
        state = self.state = self.observer.update(view.frame, self.roi, now)
        if self.outside_watch is not None:
            boxes = [] if state.error else state.boxes
            if (
                self.outside_watch.update(boxes, view.frame.shape, now)
                and self.collector is not None
                and self.collector.trigger(source="outside_stay") is not None
            ):
                self.outside_watch.recorded = True
        # 推論に失敗した間も、犬がいる可能性があるので判定を保留する。
        if state.error or state.on_roi:
            view.observation.kind = "occupied"
        self._record_transition(state)

    def _record_transition(self, state: "DogState") -> None:
        """入場・退場の前後を、種類ごとに dog_clip_cooldown_seconds の間隔で録画する。"""
        transition = state.transition
        if self.collector is None or not transition:
            return
        last = self.clip_times.get(transition, float("-inf"))
        if (
            self.clock.monotonic() - last >= self.config.dog_clip_cooldown_seconds
            and self.collector.trigger(source=transition) is not None
        ):
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


@dataclass
class LoopState:
    """フレームをまたいで保つ判定の状態。中断時のリセットはここに集める。"""

    machine: Machine
    generation: int
    # 候補が続き始めた時刻（UTC）。記録するイベントの detection_started になる。
    started_at: str | None = None
    # 空の判定が続き始めた時刻。clear_seconds 続いたら通知済み状態を更新する。
    settled_since: float | None = None
    # 直前の切り出し画像。フレーム間の動きを確かめるのに使う。
    previous: Frame | None = None
    tray_state: str | None = None
    extensions: Sequence[MonitorExtension] = field(default_factory=tuple)

    def reset_timers(self) -> None:
        """フレーム間の動きなどで、継続確認を最初からやり直す。"""
        self.machine.interrupt()
        self.started_at = None
        self.settled_since = None

    def interrupt(self) -> None:
        """再接続や追従の失敗で、続いていた判定をすべて捨てる。"""
        self.reset_timers()
        self.previous = None
        for extension in self.extensions:
            extension.on_interrupt()


class Monitor:
    """監視ループの本体。step が1回分の読み込みと処理を行う。"""

    def __init__(
        self,
        camera: FrameSource,
        roi: AnyROI,
        baseline: Frame,
        config: Config,
        notifier: MonitorNotifier,
        *,
        collector: ClipCollector | None = None,
        review_notifications: bool = False,
        tray_tracker: TrayTracker | None = None,
        on_tray_state: Callable[[str], None] | None = None,
        extensions: Sequence[MonitorExtension] = (),
        clock: Clock | None = None,
    ) -> None:
        self.camera, self.roi, self.config, self.notifier = camera, roi, config, notifier
        self.collector, self.review_notifications = collector, review_notifications
        self.tray_tracker, self.on_tray_state = tray_tracker, on_tray_state
        self.extensions = tuple(extensions)
        self.clock = clock or SystemClock()
        self.detector = IncrementalDetector(
            baseline,
            config.threshold,
            config.min_area,
            config.occupied_ratio,
            config.color_threshold,
        )
        self.store = Store(config.data_dir)
        self.signature = scene_signature(baseline, roi)
        if collector is not None:
            collector.scene_signature = self.signature
        # 再起動前に通知した跡を覚えていれば、その跡からの追加分だけを評価する。
        reference = self.store.load_reference(self.signature)
        if reference is not None:
            self.detector.accept(reference)
        self.state = LoopState(self._new_machine(), camera.generation, extensions=self.extensions)
        self.process = psutil.Process()
        self.process.cpu_percent()
        self.last_health = self.clock.monotonic()

    def _new_machine(self) -> Machine:
        return Machine(self.config.verify_seconds, self.config.clear_seconds)

    def run(self, max_frames: int = 0) -> int:
        """max_frames が 0 なら止めるまで動き、処理したフレーム数を返す。"""
        seen = 0
        try:
            while not max_frames or seen < max_frames:
                seen += self.step()
        except KeyboardInterrupt:
            logger.info("監視を終了しました")
        return seen

    def step(self) -> int:
        """1回読み込んで処理する。カメラから新しいフレームが届いたら 1 を返す。"""
        tick = self.clock.monotonic()
        if self.collector is not None:
            self.collector.poll()
        raw, frame, interrupted = self._read(tick)
        if interrupted:
            self.state.interrupt()
        self._report_tray_state(frame, interrupted)
        if raw is None or frame is None:
            self.clock.sleep(1 / self.config.fps)
            return int(raw is not None)
        view = self._observe(raw, frame, tick)
        self._settle(view)
        self._update_reference(view)
        self._log_health(tick)
        self.clock.sleep(max(0, 1 / self.config.fps - (self.clock.monotonic() - tick)))
        return 1

    def _read(self, tick: float) -> tuple[Frame | None, Frame | None, bool]:
        """読み込みと位置合わせ。(読んだフレーム, 位置合わせ後, 中断したか) を返す。"""
        raw = self.camera.read()
        reconnected = self.camera.generation != self.state.generation
        self.state.generation = self.camera.generation
        disconnected = reconnected or (raw is None and not self.camera.healthy)
        for extension in self.extensions:
            extension.on_camera_frame(raw, disconnected, tick)
        if raw is None:
            # 正常なカメラで新しいフレームを待っているだけなら、判定を続ける。
            return None, None, disconnected
        frame = raw if self.tray_tracker is None else self.tray_tracker.align(raw)
        return raw, frame, reconnected or frame is None

    def _report_tray_state(self, frame: Frame | None, interrupted: bool) -> None:
        if self.tray_tracker is None or (frame is None and not interrupted):
            return
        state = "searching" if frame is None else "tracking"
        if state != self.state.tray_state:
            self.state.tray_state = state
            if self.on_tray_state is not None:
                self.on_tray_state(state)

    def _observe(self, raw: Frame, frame: Frame, tick: float) -> FrameView:
        valid_mask = self.roi.valid_mask(frame.shape)
        crop = self.roi.crop(frame)
        observation = self.detector.analyze(crop, valid_mask)
        view = FrameView(raw, frame, crop, valid_mask, observation, tick)
        for extension in self.extensions:
            extension.on_frame(view)
        return view

    def _settle(self, view: FrameView) -> None:
        """フレーム間の動きを確かめてから状態を進め、候補が続いたら記録する。"""
        state, observation = self.state, view.observation
        moving = state.previous is not None and self.detector.changed_from(
            view.crop, state.previous, view.valid_mask
        )
        state.previous = view.crop.copy()
        if moving:
            state.reset_timers()
            if observation.kind == "candidate":
                observation.kind = "unstable"
        if observation.kind == "candidate" and state.machine.started is None:
            state.started_at = utcnow()
        if observation.kind != "candidate":
            state.started_at = None
        if state.machine.update(observation.kind, self.clock.monotonic()):
            self._record(view)

    def _record(self, view: FrameView) -> None:
        """イベントを記録し、その跡を通知済み状態にしてから、動画保存と通知へ進む。"""
        event_id = self.store.record(
            view.raw,
            view.observation.score,
            self.state.started_at,
            (self.clock.monotonic() - view.tick) * 1000,
            reference=view.crop,
            signature=self.signature,
        )
        self.detector.accept(view.crop)
        self.state.machine = self._new_machine()
        self.state.started_at = None
        self.state.settled_since = None
        self.store.set_monitor_state("SUSPECTED", event_id)
        if self.collector is not None:
            self.collector.trigger(event_id)
        if not self.review_notifications and self.notifier.send():
            self.store.update(event_id, notified_at=utcnow())
        logger.info("排泄の疑いを記録しました: %s", event_id)

    def _update_reference(self, view: FrameView) -> None:
        """空が clear_seconds 続き、通知済み状態と違えば（清掃・乾燥など）更新する。"""
        if view.observation.kind != "empty":
            self.state.settled_since = None
            return
        if self.state.settled_since is None:
            self.state.settled_since = self.clock.monotonic()
        if self.clock.monotonic() - self.state.settled_since >= self.config.clear_seconds and (
            self.detector.changed_from(view.crop, self.detector.reference_image, view.valid_mask)
        ):
            self.store.save_reference(view.crop, self.signature)
            self.detector.accept(view.crop)

    def _log_health(self, tick: float) -> None:
        if self.clock.monotonic() - self.last_health < HEALTH_LOG_SECONDS:
            return
        logger.info(
            "状態=%s CPU=%.1f%% RSS=%.1fMiB 処理=%.1fms",
            self.state.machine.state,
            self.process.cpu_percent(),
            self.process.memory_info().rss / 1024**2,
            (self.clock.monotonic() - tick) * 1000,
        )
        self.last_health = self.clock.monotonic()
        for extension in self.extensions:
            extension.log_status()


def dog_observer_from_config(config: Config) -> "DogObserver":
    """設定の犬検出モデルを読み込む。モデルがなければ準備の方法をログに出して送出する。"""
    from ..features.dog import DogDetector, DogObserver

    try:
        model = DogDetector(Path(config.data_dir) / "models" / "dog-yolox", config.dog_confidence)
    except (OSError, ValueError):
        logger.error("犬のモデルを準備してください: uv run poop-detector setup-dog-model")
        raise
    return DogObserver(model, config.dog_fps, config.dog_exit_seconds)


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
) -> list[MonitorExtension]:
    """CLI の --early-alert・--dog・--detect-outside・--activity-map に対応する拡張を作る。"""
    extensions: list[MonitorExtension] = []
    if dog_observer is None and config.dog_enabled:
        dog_observer = dog_observer_from_config(config)
    if activity_map is not None:
        if dog_observer is None:
            raise ValueError("行動マップには犬の検出が必要です")
        # 行動マップは位置合わせ前の映像を使うので、カメラの読み込み直後に呼ばれる。
        extensions.append(ActivityExtension(activity_map, dog_observer.predict))
    if early_alert:
        extensions.append(EarlyAlertExtension(EarlyAlert(baseline, config), notifier, collector))
    if dog_observer is not None:
        watch = None
        if outside_roi is not None:
            from ..experimental.outside import OutsideWatch

            watch = OutsideWatch(roi, outside_roi, config.dog_exit_seconds)
        extensions.append(DogExtension(dog_observer, roi, config, clock, collector, watch))
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
    *,
    extensions: Sequence[MonitorExtension] = (),
    clock: Clock | None = None,
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
    ).run(max_frames)
