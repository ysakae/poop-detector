"""カメラのフレームを順に判定し、記録・通知・動画保存へつなぐ監視ループ。

1フレームの処理は次の段に分かれる。

1. 読み込み: カメラからフレームを読み、再接続や接続の途切れを中断として扱う。
2. 位置合わせ: トレー追従で登録時の座標へ戻す。合わせられなければ中断。
3. 観測: ROI で切り出して差分を判定し、拡張（早期通知、犬の検出など）が観測を上書きする。
4. 状態遷移: フレーム間の動きを確かめ、候補が続いたら記録・通知・動画保存へ進む。
5. 通知済み状態の更新: 清掃などで空が続いたら、通知済みの状態を空に戻す。

任意の機能は MonitorExtension として差し込み、Monitor の本体を変えずに足せる。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

import psutil

from ..config import Config
from ..frames import Frame, Mask
from ..records.events import Store, utcnow
from .difference import IncrementalDetector, Observation
from .roi import AnyROI
from .scene import scene_signature
from .state_machine import Machine

if TYPE_CHECKING:
    from ..features.tray_tracking import TrayTracker
    from ..video.recording import ClipCollector

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


class DetectionNotifier(Protocol):
    """差分検知の結果を知らせる通知先。"""

    def send(self, *, clip_id: str | None = None, detected_at: str | None = None) -> bool: ...


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

    def on_event(
        self, db: sqlite3.Connection, event_id: str, view: FrameView, detector: IncrementalDetector
    ) -> None:
        """排泄候補を記録した直後のフレームと更新前の基準を受け取る。"""

    def on_reference(
        self,
        view: FrameView,
        detector: IncrementalDetector,
        reason: Literal["event", "settled"],
    ) -> None:
        """通知済み状態を更新した直後に、更新後の基準を受け取る。

        reason は、排泄候補の記録（event）か、清掃・乾燥で空が続いたこと（settled）。
        """

    def log_status(self) -> None:
        """稼働ログ（HEALTH_LOG_SECONDS ごと）に、機能ごとの状態を書く。"""


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
        notifier: DetectionNotifier,
        *,
        collector: ClipCollector | None = None,
        review_notifications: bool = False,
        tray_tracker: TrayTracker | None = None,
        on_tray_state: Callable[[str], None] | None = None,
        extensions: Sequence[MonitorExtension] = (),
        clock: Clock | None = None,
        before_align: Callable[[Monitor, Frame | None], bool] | None = None,
    ) -> None:
        self.camera, self.roi, self.config, self.notifier = camera, roi, config, notifier
        self.collector, self.review_notifications = collector, review_notifications
        self.tray_tracker, self.on_tray_state = tray_tracker, on_tray_state
        self.extensions = tuple(extensions)
        self.clock = clock or SystemClock()
        self.before_align = before_align
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

    def replace_scene(
        self,
        roi: AnyROI,
        baseline: Frame,
        tracker: TrayTracker | None,
        extensions: Sequence[MonitorExtension],
    ) -> None:
        """交換前の判定と参照画像を捨て、新しい場面の監視を開始する。"""
        self.state.interrupt()
        self.roi, self.tray_tracker = roi, tracker
        self.detector = IncrementalDetector(
            baseline,
            self.config.threshold,
            self.config.min_area,
            self.config.occupied_ratio,
            self.config.color_threshold,
        )
        self.signature = scene_signature(baseline, roi)
        self.extensions = tuple(extensions)
        self.state = LoopState(
            self._new_machine(), self.camera.generation, extensions=self.extensions
        )
        if self.collector is not None:
            self.collector.scene_signature = self.signature

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
        if self.before_align is not None and self.before_align(self, raw):
            return raw, None, True
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
        detected_at = self.state.started_at or utcnow()

        def record_details(db: sqlite3.Connection, event_id: str) -> None:
            for extension in self.extensions:
                extension.on_event(db, event_id, view, self.detector)

        event_id = self.store.record(
            view.raw,
            view.observation.score,
            self.state.started_at,
            (self.clock.monotonic() - view.tick) * 1000,
            reference=view.crop,
            signature=self.signature,
            record_details=record_details,
        )
        self.detector.accept(view.crop)
        for extension in self.extensions:
            extension.on_reference(view, self.detector, "event")
        self.state.machine = self._new_machine()
        self.state.started_at = None
        self.state.settled_since = None
        clip_id = self.collector.trigger(event_id) if self.collector else None
        if not self.review_notifications and self.notifier.send(
            clip_id=clip_id, detected_at=detected_at
        ):
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
            for extension in self.extensions:
                extension.on_reference(view, self.detector, "settled")

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
