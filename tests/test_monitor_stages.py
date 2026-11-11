"""監視ループの段と拡張を、それぞれ単体で検証する。"""

import numpy as np
from support import FrameSource, SilentNotifier, spot_scene

from poop_detector.config import Config
from poop_detector.detection.difference import Observation
from poop_detector.detection.monitoring import (
    ActivityExtension,
    DogExtension,
    FrameView,
    LoopState,
    Monitor,
    MonitorExtension,
    monitor,
)
from poop_detector.detection.roi import ROI
from poop_detector.detection.state_machine import Machine
from poop_detector.features.dog import DogState

EMPTY, STAIN = spot_scene()


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Recorder(MonitorExtension):
    """呼ばれた呼び出し口を記録する拡張。"""

    def __init__(self, occupy=False):
        self.calls = []
        self.occupy = occupy

    def on_camera_frame(self, raw, disconnected, tick):
        self.calls.append(("camera", raw is not None, disconnected))

    def on_interrupt(self):
        self.calls.append(("interrupt",))

    def on_frame(self, view):
        self.calls.append(("frame", view.observation.kind))
        if self.occupy:
            view.observation.kind = "occupied"


def view(kind="candidate"):
    return FrameView(STAIN, STAIN, STAIN, None, Observation(kind, 0.1), 0.0)


def test_loop_state_interrupt_resets_everything_and_tells_extensions():
    extension = Recorder()
    state = LoopState(Machine(), 1, extensions=[extension])
    state.machine.update("candidate", 0)
    state.started_at, state.settled_since, state.previous = "2026-11-01T00:00:00+00:00", 1.0, STAIN
    state.interrupt()
    assert state.machine.started is None
    assert (state.started_at, state.settled_since, state.previous) == (None, None, None)
    assert extension.calls == [("interrupt",)]


def test_reset_timers_keeps_previous_frame():
    state = LoopState(Machine(), 1, previous=STAIN)
    state.started_at = "2026-11-01T00:00:00+00:00"
    state.reset_timers()
    assert state.started_at is None
    assert state.previous is STAIN


def test_custom_extension_can_hold_detection_without_changing_monitor(tmp_path):
    """新しい機能は extensions に渡すだけで足せる。"""
    extension = Recorder(occupy=True)
    config = Config(data_dir=str(tmp_path), fps=100, verify_seconds=0.01)
    notifier = SilentNotifier()
    monitor(
        FrameSource(STAIN),
        ROI(0, 0, 1, 1),
        EMPTY,
        config,
        notifier,
        max_frames=5,
        extensions=[extension],
        clock=Clock(),
    )
    assert notifier.sent == 0
    assert extension.calls[:2] == [("camera", True, False), ("frame", "candidate")]


def test_monitor_step_reports_new_frames_and_reconnects(tmp_path):
    class Camera:
        generation = 1
        healthy = False

        def __init__(self):
            self.frames = [STAIN, None, STAIN]

        def read(self):
            frame = self.frames.pop(0)
            if not self.frames:
                self.generation = 2
            return frame

    extension = Recorder()
    clock = Clock()
    loop = Monitor(
        Camera(),
        ROI(0, 0, 1, 1),
        EMPTY,
        Config(data_dir=str(tmp_path), fps=10),
        SilentNotifier(),
        extensions=[extension],
        clock=clock,
    )
    assert [loop.step(), loop.step(), loop.step()] == [1, 0, 1]
    # 2回目は接続の途切れ、3回目は再接続。どちらも中断として扱う。
    assert [call for call in extension.calls if call[0] != "frame"] == [
        ("camera", True, False),
        ("camera", False, True),
        ("interrupt",),
        ("camera", True, True),
        ("interrupt",),
    ]
    # フレームが届かない回は 1/fps 秒待つ。
    assert clock.now >= 0.1


def test_dog_extension_holds_detection_and_limits_clip_triggers():
    class Observer:
        def __init__(self, states):
            self.states = list(states)
            self.resets = 0

        def update(self, frame, roi, now):
            return self.states.pop(0)

        def reset(self):
            self.resets += 1

    class Collector:
        def __init__(self):
            self.sources = []

        def trigger(self, *, source):
            self.sources.append(source)
            return "clip"

    enter = DogState(True, True, "dog_enter", [], 1)
    clock = Clock()
    collector = Collector()
    observer = Observer([enter, enter, DogState(True, False, None, [], 1, error=True)])
    config = Config(dog_clip_cooldown_seconds=60)
    extension = DogExtension(observer, ROI(0, 0, 1, 1), config, clock, collector)

    first = view()
    extension.on_frame(first)
    assert first.observation.kind == "occupied"
    clock.now = 30
    extension.on_frame(view())
    # 推論に失敗した間も保留する。
    failed = view()
    extension.on_frame(failed)
    assert failed.observation.kind == "occupied"
    assert collector.sources == ["dog_enter"]
    extension.on_interrupt()
    assert observer.resets == 1


def test_activity_extension_only_breaks_paths_on_disconnect():
    calls = []

    class Recorder:
        def interrupt(self):
            calls.append("interrupt")

        def update(self, frame, predict, tick):
            calls.append(("update", predict(frame), tick))

    extension = ActivityExtension(Recorder(), lambda frame: [(1, 2, 3, 4)])
    extension.on_camera_frame(STAIN, False, 1.0)
    extension.on_camera_frame(None, True, 2.0)
    # トレー追従の失敗では区切らない（on_interrupt は何もしない）。
    extension.on_interrupt()
    assert calls == [("update", [(1, 2, 3, 4)], 1.0), "interrupt"]


def test_health_log_includes_extension_status(tmp_path, caplog):
    class Status(MonitorExtension):
        def log_status(self):
            import logging

            logging.getLogger("poop_detector.detection.monitoring").info("拡張の状態")

    caplog.set_level("INFO", logger="poop_detector.detection.monitoring")
    clock = Clock()
    loop = Monitor(
        FrameSource(np.full_like(EMPTY, 200)),
        ROI(0, 0, 1, 1),
        EMPTY,
        Config(data_dir=str(tmp_path), fps=10),
        SilentNotifier(),
        extensions=[Status()],
        clock=clock,
    )
    loop.step()
    assert "拡張の状態" not in caplog.text
    clock.now += 31
    loop.step()
    assert "状態=EMPTY" in caplog.text
    assert "拡張の状態" in caplog.text
