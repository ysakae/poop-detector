"""トレーの追従と、位置を確認できない間の検知保留を検証する。"""

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from poop_detector.config import Config
from poop_detector.detection.monitoring import monitor
from poop_detector.detection.roi import ROI
from poop_detector.features.early_alert import EarlyAlert
from poop_detector.features.tray_tracking import TrayTracker
from poop_detector.main import parser
from poop_detector.video.codex_cli import CodexCliJudge


def tray_scene():
    frame = np.full((360, 640, 3), 65, np.uint8)
    cv2.rectangle(frame, (170, 75), (489, 284), (222, 222, 222), -1)
    cv2.rectangle(frame, (186, 91), (473, 268), (140, 150, 165), -1)
    for x in range(188, 471, 30):
        for y in (81, 276):
            cv2.rectangle(frame, (x, y), (x + 8, y + 4), (115, 115, 115), -1)
    roi = ROI.from_pixels((170, 75, 320, 210), 640, 360)
    return frame, roi


def test_tracks_tray_when_the_background_stays_still():
    frame, roi = tray_scene()
    cv2.rectangle(frame, (60, 55), (115, 300), (188, 195, 205), -1)
    tracker = TrayTracker(frame, roi)
    background = frame.copy()
    background[75:285, 170:490] = 65
    transform = cv2.getRotationMatrix2D((330, 180), 4, 1.02)
    transform[:, 2] += [16, -11]
    moving_mask = np.zeros(frame.shape[:2], np.uint8)
    moving_mask[75:285, 170:490] = 255
    mask = cv2.warpAffine(moving_mask, transform, (640, 360))
    moved = cv2.warpAffine(frame, transform, (640, 360))
    background[mask > 0] = moved[mask > 0]
    aligned = tracker.align(background)
    assert aligned is not None
    assert np.abs(roi.crop(aligned).astype(float) - roi.crop(frame)).mean() < 4


def test_camera_shift_keeps_new_changes_and_recovers_after_occlusion():
    frame, roi = tray_scene()
    tracker = TrayTracker(frame, roi)
    assert tracker.align(np.full_like(frame, 70)) is None
    transform = np.array([[1, 0.02, -18], [0.01, 1, -11]], dtype=np.float32)
    dirty = frame.copy()
    cv2.circle(dirty, (326, 187), 9, (35, 47, 63), -1)
    shifted = cv2.warpAffine(dirty, transform, (640, 360))
    aligned = tracker.align(shifted)
    assert aligned is not None
    assert np.abs(roi.crop(aligned).astype(float) - roi.crop(dirty)).mean() < 3
    assert aligned[187, 326].tolist() == [35, 47, 63]


def test_tracks_shifted_tray_when_lighting_changes():
    frame, roi = tray_scene()
    tracker = TrayTracker(frame, roi)
    dimmed = (frame * 0.72).astype(np.uint8)
    shifted = cv2.warpAffine(dimmed, np.float32([[1, 0, 12], [0, 1, -8]]), (640, 360))

    aligned = tracker.align(shifted)

    assert aligned is not None
    assert np.abs(roi.crop(aligned).astype(float) - roi.crop(dimmed)).mean() < 1


def test_tracks_tray_when_infrared_reverses_rim_contrast():
    frame, roi = tray_scene()
    tracker = TrayTracker(frame, roi)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    infrared = cv2.cvtColor(255 - gray, cv2.COLOR_GRAY2BGR)
    cv2.circle(infrared, (340, 186), 7, (35, 35, 35), -1)
    shifted = cv2.warpAffine(infrared, np.float32([[1, 0, 10], [0, 1, -6]]), (640, 360))

    aligned = tracker.align(shifted)

    assert aligned is not None
    assert np.abs(roi.crop(aligned).astype(float) - roi.crop(infrared)).mean() < 2
    assert aligned[186, 340].tolist() == [35, 35, 35]


def test_pauses_when_the_tray_leaves_the_frame():
    frame, _ = tray_scene()
    edge_roi = ROI.from_pixels((170, 75, 320, 285), 640, 360)
    tracker = TrayTracker(frame, edge_roi)
    shifted = cv2.warpAffine(frame, np.float32([[1, 0, 0], [0, 1, 18]]), (640, 360))
    assert tracker.align(shifted) is None


def test_monitor_pauses_and_resumes_early_alert(tmp_path, monkeypatch):
    frame, roi = tray_scene()
    updates = []
    notices = []
    states = []

    class Camera:
        generation = 1
        healthy = True

        def read(self):
            return frame

    class Tracker:
        def __init__(self):
            self.frames = iter([None, frame, None, frame])

        def align(self, frame):
            return next(self.frames)

    class Notifier:
        def send_early_alert(self):
            notices.append("early")
            return True

    monkeypatch.setattr(EarlyAlert, "update", lambda self, *args: updates.append("frame") or True)
    assert (
        monitor(
            Camera(),
            roi,
            roi.crop(frame),
            Config(data_dir=str(tmp_path), fps=100),
            Notifier(),
            max_frames=4,
            early_alert=True,
            tray_tracker=Tracker(),
            on_tray_state=states.append,
        )
        == 4
    )
    assert updates == ["frame", "frame"]
    assert notices == ["early", "early"]
    assert states == ["searching", "tracking", "searching", "tracking"]


def test_tracking_flag_is_available_for_capture_monitor_and_review():
    for command in ("capture-baseline", "run", "codex-review"):
        assert parser().parse_args([command, "--track-tray"]).track_tray


def test_missing_tray_in_clip_holds_codex_judgment(tmp_path):
    from poop_detector.video.review import inspect_clip

    frame, roi = tray_scene()
    tracker = TrayTracker(frame, roi)
    path = tmp_path / "hidden.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, (640, 360))
    assert writer.isOpened()
    try:
        for _ in range(10):
            writer.write(np.full_like(frame, 70))
    finally:
        writer.release()

    def no_codex(*args, **kwargs):
        raise AssertionError("トレーが見えない動画はCodexに送らない")

    assessment = inspect_clip(
        path, {"duration": 2}, roi, judge=CodexCliJudge(no_codex), tray_tracker=tracker
    )
    assert assessment.label == "uncertain"
    assert "トレーの位置" in assessment.reason


def test_tray_reference_must_match_current_baseline(tmp_path):
    from poop_detector.features.tray_tracking import TrayReferenceError

    frame, roi = tray_scene()
    reference = tmp_path / "tray.png"
    assert cv2.imwrite(str(reference), frame)
    baseline = roi.crop(frame).copy()
    assert TrayTracker.from_baseline(reference, baseline, roi).align(frame) is not None
    baseline[25, 25] = 0
    with pytest.raises(TrayReferenceError, match="capture-baseline"):
        TrayTracker.from_baseline(reference, baseline, roi)


def test_plain_white_rim_can_be_registered_without_markers():
    frame = np.full((360, 640, 3), 65, np.uint8)
    cv2.rectangle(frame, (170, 75), (489, 284), (222, 222, 222), -1)
    cv2.rectangle(frame, (195, 100), (464, 259), (140, 150, 165), -1)
    roi = ROI.from_pixels((170, 75, 320, 210), 640, 360)
    tracker = TrayTracker(frame, roi)
    shifted = cv2.warpAffine(frame, np.float32([[1, 0, 15], [0, 1, -10]]), (640, 360))
    aligned = tracker.align(shifted)
    assert aligned is not None
    assert np.abs(roi.crop(aligned).astype(float) - roi.crop(frame)).mean() < 1


@pytest.mark.parametrize(("evidence_at", "label"), [(3, "uncertain"), (5, "feces")])
def test_codex_evidence_must_come_from_a_tracked_frame(tmp_path, evidence_at, label):
    from poop_detector.video.review import inspect_clip

    frame, roi = tray_scene()
    path = tmp_path / "partial.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 1, (640, 360))
    assert writer.isOpened()
    try:
        for _ in range(6):
            writer.write(frame)
    finally:
        writer.release()

    class Tracker:
        def __init__(self):
            self.visible = iter([True, True, False, True])

        def align(self, image):
            return image if next(self.visible) else None

    def runner(*args, cwd, **kwargs):
        (Path(cwd) / "result.json").write_text(
            json.dumps(
                {
                    "label": "feces",
                    "activity": "excreting",
                    "start_seconds": None,
                    "end_seconds": evidence_at,
                    "reason": "新しい便が見えます",
                    "evidence": "new_stool",
                }
            )
        )

    assert (
        inspect_clip(
            path, {"duration": 6}, roi, judge=CodexCliJudge(runner), tray_tracker=Tracker()
        ).label
        == label
    )


def edge_scene():
    """ROI の下端が追従用の縮小画像（高さ360px）の最下段 359px に接する場面。"""
    frame, _ = tray_scene()
    return frame, ROI.from_pixels((170, 75, 320, 285), 640, 360)


def shift_down(frame, pixels):
    return cv2.warpAffine(
        frame,
        np.float32([[1, 0, 0], [0, 1, pixels]]),
        (640, 360),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


@pytest.mark.parametrize("pixels", [0.6, 1.3, 2.5])
def test_tracks_when_roi_overhangs_the_frame_edge_slightly(pixels):
    frame, roi = edge_scene()
    tracker = TrayTracker(frame, roi)

    aligned = tracker.align(shift_down(frame, pixels))

    assert aligned is not None
    assert np.abs(roi.crop(aligned).astype(float) - roi.crop(frame)).mean() < 2


def test_filler_beyond_the_frame_edge_comes_from_the_reference():
    frame, roi = edge_scene()
    tracker = TrayTracker(frame, roi)
    shifted = shift_down(frame, 1.3)
    # 実映像の最下段だけが変わった。補正後に画面外へ出る行は、この画素で埋めない。
    shifted[359] = 250
    cv2.circle(shifted, (330, 200), 8, (35, 47, 63), -1)

    aligned = tracker.align(shifted)

    assert aligned is not None
    assert aligned[358:].tolist() == frame[358:].tolist()
    # 画面内に残っている新しい跡は使える。
    assert aligned[199, 330].tolist() == [35, 47, 63]


def test_pauses_when_the_overhang_exceeds_the_tolerance():
    frame, roi = edge_scene()
    tracker = TrayTracker(frame, roi)

    assert tracker.align(shift_down(frame, 6)) is None
    assert tracker.last_failure == "outside"


def test_reports_why_tracking_paused():
    frame, roi = edge_scene()
    tracker = TrayTracker(frame, roi)

    assert tracker.align(np.full_like(frame, 70)) is None
    assert tracker.last_failure == "unmatched"
    assert tracker.align(shift_down(frame, 1.3)) is not None
    assert tracker.last_failure is None


@pytest.mark.parametrize(("pixels", "state"), [(1.3, "tracking"), (18, "searching")])
def test_monitor_tracks_slight_overhang_and_pauses_outside(tmp_path, pixels, state):
    frame, roi = edge_scene()
    shifted = shift_down(frame, pixels)
    states = []

    class Camera:
        generation = 1
        healthy = True

        def read(self):
            return shifted

    class Notifier:
        def send(self, *args, **kwargs):
            return True

    monitor(
        Camera(),
        roi,
        roi.crop(frame),
        Config(data_dir=str(tmp_path), fps=100),
        Notifier(),
        max_frames=3,
        tray_tracker=TrayTracker(frame, roi),
        on_tray_state=states.append,
    )
    assert states == [state]


@pytest.mark.parametrize(("pixels", "judged"), [(1.3, True), (18, False)])
def test_saved_clip_tracks_slight_overhang_and_holds_outside(tmp_path, pixels, judged):
    from poop_detector.video.review import inspect_clip

    frame, roi = edge_scene()
    path = tmp_path / "edge.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 2, (640, 360))
    assert writer.isOpened()
    try:
        for _ in range(6):
            writer.write(shift_down(frame, pixels))
    finally:
        writer.release()
    calls = []

    def runner(*args, cwd, **kwargs):
        calls.append(cwd)
        (Path(cwd) / "result.json").write_text(
            json.dumps(
                {
                    "label": "none",
                    "activity": "other",
                    "start_seconds": None,
                    "end_seconds": None,
                    "reason": "変化はありません",
                    "evidence": "none",
                }
            )
        )

    assessment = inspect_clip(
        path,
        {"duration": 3},
        roi,
        judge=CodexCliJudge(runner),
        tray_tracker=TrayTracker(frame, roi),
    )
    assert bool(calls) is judged
    assert assessment.label == ("none" if judged else "uncertain")
