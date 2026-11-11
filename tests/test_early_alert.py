"""トイレ滞在の早期通知と、検知後の録画開始を検証する。"""

import numpy as np

from poop_detector.config import Config
from poop_detector.early_alert import EarlyAlert
from poop_detector.main import parser
from poop_detector.monitoring import monitor
from poop_detector.roi import ROI


def frames():
    empty = np.full((160, 200, 3), 190, dtype=np.uint8)
    stays = empty.copy()
    stays[35:95, 25:85] = 75
    moved = empty.copy()
    moved[35:95, 115:175] = 75
    return empty, stays, moved


def test_alert_requires_a_stay_in_one_place_and_only_fires_once():
    empty, stays, moved = frames()
    alert = EarlyAlert(empty, Config())
    for t, frame in enumerate((stays, moved, stays, stays, stays, stays)):
        assert not alert.update(frame, None, t)
    assert alert.update(stays, None, 6)
    for t in range(7, 40):
        assert not alert.update(stays, None, t)
    assert not alert.update(empty, None, 40)
    for t in range(41, 45):
        assert not alert.update(moved, None, t)
    assert alert.update(moved, None, 45)


def test_stay_and_cooldown_come_from_the_config():
    """early_alert.stay_seconds と cooldown_seconds で、通知までの滞在と間隔を変えられる。"""
    empty, stays, moved = frames()
    alert = EarlyAlert(empty, Config(early_alert_stay_seconds=6, early_alert_cooldown_seconds=60))
    for t, frame in enumerate((stays, moved, stays, stays, stays, stays, stays, stays)):
        assert not alert.update(frame, None, t), t
    assert alert.update(stays, None, 8)
    assert not alert.update(empty, None, 40)
    # 新しい場所での滞在でも、前の通知から60秒たつまでは通知しない。
    assert not any(alert.update(moved, None, t) for t in range(41, 66))
    assert not alert.update(empty, None, 66)
    assert not any(alert.update(stays, None, t) for t in range(67, 73))
    assert alert.update(stays, None, 73)


def test_gap_and_masked_motion_do_not_trigger():
    empty, stays, _ = frames()
    alert = EarlyAlert(empty, Config())
    assert not alert.update(stays, None, 0)
    assert not alert.update(stays, None, 1)
    assert not alert.update(stays, None, 10)
    for t in range(11, 14):
        assert not alert.update(stays, None, t)
    mask = np.ones(empty.shape[:2], dtype=np.uint8)
    mask[:, :100] = 0
    alert.reset()
    for t in range(14, 25):
        assert not alert.update(stays, mask, t)


def test_cooldown_suppresses_a_new_stay_immediately_after_alert():
    empty, stays, moved = frames()
    alert = EarlyAlert(empty, Config())
    assert not alert.update(empty, None, -1)
    for t in range(4):
        assert not alert.update(stays, None, t)
    assert alert.update(stays, None, 4)
    for t in range(5, 16):
        assert not alert.update(moved, None, t)


def test_monitor_sends_and_records_early_alert_alongside_codex(tmp_path, monkeypatch):
    empty, stays, _ = frames()
    calls = []

    class Camera:
        generation = 1
        healthy = True

        def read(self):
            return stays.copy()

    class Notifier:
        def send_early_alert(self):
            calls.append("notice")
            return True

    class Collector:
        def poll(self):
            pass

        def trigger(self, *, source):
            calls.append(source)

    monkeypatch.setattr(EarlyAlert, "update", lambda *args: True)
    monitor(
        Camera(),
        ROI(0, 0, 1, 1),
        empty,
        Config(data_dir=str(tmp_path), fps=100),
        Notifier(),
        max_frames=1,
        collector=Collector(),
        codex_notifications=True,
        early_alert=True,
    )
    assert calls == ["notice", "early_alert"]
    assert parser().parse_args(["run", "--codex", "--early-alert"]).early_alert
    assert not parser().parse_args(["run"]).early_alert


def test_global_lighting_change_does_not_trigger():
    empty, _, _ = frames()
    alert = EarlyAlert(empty, Config())
    darker = np.full_like(empty, 75)
    assert not any(alert.update(darker, None, t) for t in range(8))


def test_already_resting_or_small_movements_do_not_trigger_but_new_stay_does():
    empty, stays, moved = frames()
    breathing = stays.copy()
    breathing[55:62, 55:62] = 105
    alert = EarlyAlert(empty, Config())
    for tick in range(12):
        assert not alert.update(stays if tick % 2 else breathing, None, tick)
    for tick in range(12, 16):
        assert not alert.update(moved, None, tick)
    assert alert.update(moved, None, 16)


def test_connection_gap_does_not_rearm_early_alert_for_a_static_dog():
    empty, stays, _ = frames()
    alert = EarlyAlert(empty, Config())
    assert not alert.update(empty, None, 0)
    assert not alert.update(stays, None, 1)
    for tick in range(10, 20):
        assert not alert.update(stays, None, tick)
