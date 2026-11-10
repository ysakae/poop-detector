"""サークル基準の集計と行動マップの公開経路を検証する。"""

from datetime import date, datetime

import numpy as np
import pytest
from test_label_gui import gui as gui
from test_label_gui import request

from poop_detector.activity_map import ActivityLayout, ActivityRecorder, ActivityStore
from poop_detector.config import Config
from poop_detector.main import monitor, parser
from poop_detector.roi import ROI, PolygonROI


def layout():
    return ActivityLayout("a" * 32, PolygonROI([[0.1, 0.1], [0.9, 0.2], [0.8, 0.9], [0.2, 0.8]]))


def test_activity_uses_fresh_positions_and_never_connects_across_a_gap(tmp_path):
    store = ActivityStore(tmp_path)
    calibration = layout()
    recorder = ActivityRecorder(store, calibration)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    a = [(28, 28, 32, 32)]
    b = [(49, 45, 53, 49)]
    c = [(66, 62, 70, 66)]
    now = datetime.fromisoformat("2026-11-10T00:30:00+09:00")
    for tick, boxes in [(0, a), (0.5, b), (1, a), (2, b), (3, []), (4, c), (5, c), (10, a)]:
        recorder.update(frame, lambda _, boxes=boxes: boxes, tick, now)
    cells, sampled, located = store.summary(calibration.id, date(2026, 11, 10), date(2026, 11, 10))
    assert sampled == 5
    assert located == 3
    assert sum(seconds for _, _, seconds, _ in cells) == 3
    assert sum(entries for _, _, _, entries in cells) == 1
    assert store.summary(calibration.id, date(2026, 11, 9), date(2026, 11, 9)) == ([], 0, 0)


def test_activity_gui_shows_switches_and_uses_saved_floor_image(gui):
    server, store, _ = gui
    assert b"/activity" in request(server, "GET", "/")[1]
    assert "床面を登録".encode() in request(server, "GET", "/activity")[1]
    calibration = layout()
    calibration.save(store.root, np.full((100, 100, 3), 150, np.uint8))
    status, page = request(server, "GET", "/activity?mode=pass&days=7&date=2026-11-10")
    assert status == 200
    assert "通過回数".encode() in page and "集計データはまだありません".encode() in page
    assert request(server, "GET", f"/activity/background?id={calibration.id}")[0] == 200
    assert request(server, "GET", "/activity/background?id=../events.sqlite3")[0] == 404
    ActivityStore(store.root).add(calibration.id, date(2026, 11, 10), 1, (3, 4), (5, 6))
    _, page = request(server, "GET", "/activity?mode=pass&days=7&date=2026-11-10")
    assert b'<rect x="5" y="6"' in page and b'<rect x="3" y="4"' not in page
    _, page = request(server, "GET", "/activity?mode=stay&days=7&date=2026-11-10")
    assert b'<rect x="3" y="4"' in page and b'<rect x="5" y="6"' not in page


def test_monitor_observes_raw_frame_even_when_tray_is_unavailable(tmp_path):
    raw = np.full((40, 50, 3), 160, np.uint8)
    seen = []

    class Camera:
        generation = 1

        def read(self):
            return raw

    class Model:
        def predict(self, frame):
            assert frame is raw
            seen.append(frame)
            return [(16, 15, 20, 19)]

    class Observer:
        detector = Model()

        def reset(self):
            pass

    class Tray:
        def align(self, frame):
            return None

    assert (
        monitor(
            Camera(),
            ROI(0, 0, 1, 1),
            raw,
            Config(data_dir=str(tmp_path), fps=100),
            None,
            max_frames=1,
            dog_observer=Observer(),
            tray_tracker=Tray(),
            activity_map=ActivityRecorder(ActivityStore(tmp_path), layout()),
        )
        == 1
    )
    assert len(seen) == 1


def test_activity_commands_are_opt_in():
    assert parser().parse_args(["run", "--activity-map"]).activity_map
    assert parser().parse_args(["setup-activity-map", "--replace"]).replace


def test_only_one_monitor_can_record_activity(tmp_path):
    store = ActivityStore(tmp_path)
    with store.recording_lock(), pytest.raises(ValueError, match="起動中"), store.recording_lock():
        pytest.fail("二重に集計を開始できる状態です")
