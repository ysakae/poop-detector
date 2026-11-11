"""監視ループの継続確認のタイマーと、通知済み状態の扱いを固定する。

時計を差し替え、カメラから1フレーム読むたびに1秒進める。verify_seconds=2 なので、
候補が3フレーム（0・1・2秒後）続くと記録する。リセットされると、そこから3フレーム必要になる。
"""

from dataclasses import dataclass

import pytest
from support import SilentNotifier, spot_scene

from poop_detector import monitoring
from poop_detector.config import Config
from poop_detector.events import Store
from poop_detector.roi import ROI
from poop_detector.scene import scene_signature

ROI_ALL = ROI(0, 0, 1, 1)
EMPTY, STAIN = spot_scene()


@dataclass
class Step:
    """カメラが1回の読み込みで返すもの。frame が None なら新しいフレームが届かない。"""

    frame: object
    generation: int = 1
    healthy: bool = True


class ScriptedCamera:
    def __init__(self, steps, clock):
        self.steps = list(steps)
        self.clock = clock
        self.index = -1

    def _current(self):
        return self.steps[min(self.index, len(self.steps) - 1)]

    @property
    def generation(self):
        return self._current().generation if self.index >= 0 else 1

    @property
    def healthy(self):
        return self._current().healthy

    def read(self):
        self.index += 1
        self.clock.now += 1
        frame = self._current().frame
        return None if frame is None else frame.copy()


class Clock:
    now = 0.0


@pytest.fixture
def run(tmp_path, monkeypatch):
    """手順どおりにカメラを動かし、記録されたイベントの数を返す。"""
    clock = Clock()
    monkeypatch.setattr(monitoring.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(monitoring.time, "sleep", lambda _: None)
    config = Config(data_dir=str(tmp_path), fps=1, verify_seconds=2, clear_seconds=2)

    def run_steps(steps, *, max_frames=None, **kwargs):
        frames = sum(step.frame is not None for step in steps)
        monitoring.monitor(
            ScriptedCamera(steps, clock),
            ROI_ALL,
            EMPTY,
            config,
            SilentNotifier(),
            max_frames=max_frames or frames,
            **kwargs,
        )
        return len(Store(tmp_path).list())

    run_steps.store = Store(tmp_path)
    return run_steps


def stains(count, **values):
    return [Step(STAIN, **values) for _ in range(count)]


def test_candidate_is_recorded_after_verify_seconds(run):
    assert run(stains(2)) == 0
    assert run(stains(3)) == 1


def test_reconnect_resets_verification(run):
    steps = stains(2) + stains(2, generation=2)
    assert run(steps) == 0
    assert run(steps + stains(1, generation=2)) == 1


def test_missing_frame_keeps_verification_while_camera_is_healthy(run):
    steps = [Step(STAIN), Step(None, healthy=True), Step(STAIN)]
    assert run(steps) == 1


def test_missing_frame_resets_verification_when_camera_is_unhealthy(run):
    steps = [Step(STAIN), Step(None, healthy=False), *stains(2)]
    assert run(steps) == 0
    assert run([*steps, Step(STAIN)]) == 1


def test_tray_tracking_failure_resets_verification(run):
    class Tracker:
        """3フレーム目だけトレーの縁を見失う。"""

        def __init__(self):
            self.count = 0

        def align(self, frame):
            self.count += 1
            return None if self.count == 3 else frame

    states = []
    assert run(stains(5), tray_tracker=Tracker(), on_tray_state=states.append) == 0
    assert states == ["tracking", "searching", "tracking"]
    assert run(stains(6), tray_tracker=Tracker()) == 1


def test_motion_between_frames_prevents_recording(run):
    moved = EMPTY.copy()
    moved[20:41, 140:161] = 30
    assert run([Step(STAIN if index % 2 else moved) for index in range(8)]) == 0


def test_notified_stain_is_not_recorded_again_until_cleaned(run):
    assert run(stains(8)) == 1
    signature = scene_signature(EMPTY, ROI_ALL)
    assert (run.store.load_reference(signature) == STAIN).all()
    # 再起動しても通知済みの跡は覚えている。
    assert run(stains(5)) == 1
    # 清掃して clear_seconds 空が続くと、通知済み状態を空に戻し、次の排泄を記録する。
    assert run([Step(EMPTY) for _ in range(4)]) == 1
    assert (run.store.load_reference(signature) == EMPTY).all()
    assert run(stains(3)) == 2
