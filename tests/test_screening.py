"""判定の前のふるい分けの指標（記録だけで、判定は省かない）を検証する。"""

import cv2
import numpy as np
import pytest

from poop_detector.detection.roi import ROI
from poop_detector.detection.scene import save_baseline, scene_signature
from poop_detector.experimental.screening import SAMPLE_SECONDS, Screening, summarize
from poop_detector.features.dog import ScoredBox
from poop_detector.main import main
from poop_detector.video.clips import ClipStore

ROI_CENTER = ROI(0.25, 0.25, 0.5, 0.5)
SHAPE = (100, 100, 3)
ON_TRAY = (35, 35, 55, 55)
AWAY = (0, 0, 12, 12)


def frames(*scores, box=ON_TRAY):
    """1/3秒ごとの観測。None は位置合わせの失敗、0 は犬なし。"""
    for index, score in enumerate(scores):
        if score is None:
            yield index * SAMPLE_SECONDS, None
        else:
            yield index * SAMPLE_SECONDS, [ScoredBox(box, score)] if score else []


def test_low_confidence_frames_on_the_tray_are_counted_and_join_the_stay():
    # 立っている間（0.8）は確信度が高く、かがむと 0.3 に下がる。
    result = summarize(frames(0.8, 0.8, 0.3, 0.3, 0.3, 0.8, 0, 0.8), ROI_CENTER, SHAPE, 0.4)

    assert result.sampled == 8
    assert (result.tray_frames, result.weak_tray_frames) == (4, 3)
    # しきい値で数えると、かがんだ1秒あまりで滞在が切れる。低い確信度も含めると続いている
    # （1コマだけの途切れはつなぐ）。
    assert result.tray_seconds == pytest.approx(3 * SAMPLE_SECONDS)
    assert result.weak_tray_seconds == pytest.approx(8 * SAMPLE_SECONDS)
    assert result.unaligned == 0


def test_dogs_away_from_the_tray_and_alignment_failures_are_not_a_stay():
    away = summarize(frames(0.9, 0.9, box=AWAY), ROI_CENTER, SHAPE, 0.4)
    assert (away.tray_frames, away.weak_tray_frames, away.weak_tray_seconds) == (0, 0, 0)

    broken = summarize(frames(0.3, None, 0.3), ROI_CENTER, SHAPE, 0.4)
    assert broken.unaligned == 1
    assert broken.weak_tray_frames == 2
    # 1秒までの途切れはつなぐ。それより長く途切れたら、別の滞在とする。
    assert broken.weak_tray_seconds == pytest.approx(3 * SAMPLE_SECONDS)
    apart = summarize(frames(0.3, None, None, None, 0.3), ROI_CENTER, SHAPE, 0.4)
    assert apart.weak_tray_seconds == pytest.approx(SAMPLE_SECONDS)


def test_trajectory_features_use_low_confidence_positions_too():
    observations = [
        (index * SAMPLE_SECONDS, [ScoredBox((30 + index * 4, 35, 50 + index * 4, 55), 0.3)])
        for index in range(5)
    ]
    result = summarize(observations, ROI_CENTER, SHAPE, 0.4)
    assert result.located == 5
    assert result.path_length > 0
    assert result.stop_seconds == 0


def test_store_saves_screenings_and_lists_unscreened_clips(tmp_path):
    clips = ClipStore(tmp_path)
    first, second = clips.request(), clips.request()
    for clip in (first, second):
        clips.update(clip, status="ready", path=f"clips/{clip}.mp4", scene_signature="scene")
    screening = Screening(9, 4, 3, 1.0, 2.0, 0, 5, 1.5, 3.0, 0.5)

    assert clips.screenings.save(first, screening)
    assert not clips.screenings.save("missing", screening)

    saved = clips.screenings.get(first)
    assert saved is not None
    assert (saved["weak_tray_frames"], saved["weak_tray_seconds"]) == (3, 2.0)
    assert saved["version"] >= 1
    assert [row["id"] for row in clips.screenings.targets({"scene", "old"}, 10)] == [second]
    assert [row["id"] for row in clips.screenings.targets({"scene"}, 10, first)] == [first]
    assert clips.screenings.targets({"other"}, 10) == []


def test_cli_records_screenings_without_judging_or_labeling(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "data"
    frame = np.full((76, 100, 3), 150, np.uint8)
    roi = ROI(0, 0, 1, 1)
    root.mkdir()
    roi.save(root / "roi.json")
    save_baseline(root / "baseline/empty.png", frame, roi)
    clips = ClipStore(root)
    clip_id = clips.request()
    relative = f"clips/{clip_id}.mp4"
    writer = cv2.VideoWriter(str(root / relative), cv2.VideoWriter_fourcc(*"mp4v"), 3, (100, 76))
    assert writer.isOpened()
    for _ in range(6):
        writer.write(frame)
    writer.release()
    clips.update(
        clip_id,
        status="ready",
        path=relative,
        source="dog_visit",
        scene_signature=scene_signature(frame, roi),
    )
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {root}\n")
    created = []

    class DogDetector:
        def __init__(self, directory, confidence=0.4):
            created.append(confidence)

        def predict_scored(self, image):
            return [ScoredBox((40, 30, 60, 50), 0.3)]

    monkeypatch.setattr("poop_detector.features.dog.DogDetector", DogDetector)
    assert main(["--config", str(config), "video-screening", "--clip", clip_id]) == 0

    # 低い確信度まで拾える検出器を作る。
    assert created
    assert created[0] < 0.4
    saved = clips.screenings.get(clip_id)
    assert saved is not None
    assert saved["weak_tray_frames"] == saved["sampled"] > 0
    assert saved["tray_frames"] == 0
    assert clips.assessments.get(clip_id) is None
    assert clips.get(clip_id)["label"] is None
    assert clip_id in capsys.readouterr().out


def test_review_screens_each_clip_before_judging_and_survives_screening_errors(tmp_path):
    from support import ready_clip

    from poop_detector.video.clip_reviews import Assessment
    from poop_detector.video.judge_source import FixedJudge, JudgeChoice
    from poop_detector.video.review import Review

    store = ClipStore(tmp_path)
    first, second = ready_clip(store), ready_clip(store)
    for clip in (first, second):
        path = tmp_path / "clips" / f"{clip}.mp4"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"")
    order = []

    def screen(path, row):
        order.append(("screen", row["id"]))
        if row["id"] == second:
            raise ValueError("出力に含めない詳細")

    def inspect(path, row, chosen):
        order.append(("judge", row["id"]))
        return Assessment("none", "other", None, None, "排泄なし")

    class Judge:
        name = "偽の判定器"

        def check(self):
            pass

    review = Review(
        store, inspect, judges=FixedJudge(JudgeChoice(Judge(), "codex", None)), screen=screen
    )

    # ふるい分けに失敗しても、判定はそのまま続ける。
    assert review.run(limit=2) == (2, 0)
    assert sorted(order) == sorted(
        [("screen", first), ("judge", first), ("screen", second), ("judge", second)]
    )
    for clip in (first, second):
        assert order.index(("screen", clip)) < order.index(("judge", clip))


def test_screener_skips_clips_already_screened_with_this_version(tmp_path, monkeypatch):
    from poop_detector.experimental.screening import ClipScreener

    clips = ClipStore(tmp_path)
    clip_id = clips.request()
    path = tmp_path / "clips" / f"{clip_id}.mp4"
    frame = np.full((76, 100, 3), 150, np.uint8)
    path.parent.mkdir(exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 3, (100, 76))
    for _ in range(3):
        writer.write(frame)
    writer.release()
    clips.update(clip_id, status="ready", path=f"clips/{clip_id}.mp4")
    calls = []

    class DogDetector:
        def __init__(self, directory, confidence):
            calls.append("model")

        def predict_scored(self, image):
            calls.append("predict")
            return []

    monkeypatch.setattr("poop_detector.features.dog.DogDetector", DogDetector)
    screener = ClipScreener(clips.screenings, lambda row: (ROI(0, 0, 1, 1), None), tmp_path, 0.4)
    row = clips.get(clip_id)

    assert screener(path, row) is not None
    predicted = calls.count("predict")
    assert screener(path, row) is None
    assert calls.count("predict") == predicted
    # 求め直しを指定すると、同じ検出器で求め直す。
    assert screener(path, row, again=True) is not None
    assert calls.count("model") == 1
    # 求めている間に動画が整理されたら、保存せずに失敗とする。
    clips.update(clip_id, status="removed")
    with pytest.raises(OSError, match="状態"):
        screener(path, row, again=True)


def test_monitor_review_records_screenings_only_with_dog_detection(tmp_path):
    from poop_detector.commands.run import TrayScene, review_watcher
    from poop_detector.experimental.screening import ClipScreener
    from poop_detector.video.judge_source import FixedJudge, JudgeChoice

    class Collector:
        store = ClipStore(tmp_path)

    class Notifier:
        enabled = False

    roi = ROI(0, 0, 1, 1)
    initial = TrayScene(roi, np.zeros((4, 4, 3), np.uint8), None)
    judges = FixedJudge(JudgeChoice(object(), "codex", None))
    args = (Collector(), initial, {}, None, Notifier(), judges)

    assert review_watcher(*args).screen is None
    review = review_watcher(*args, screening=(tmp_path, 0.4))
    assert isinstance(review.screen, ClipScreener)
    assert review.screen.scene({"scene_signature": None}) == (roi, None)


def test_cli_reports_missing_targets_and_videos(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "data"
    frame = np.full((76, 100, 3), 150, np.uint8)
    roi = ROI(0, 0, 1, 1)
    root.mkdir()
    roi.save(root / "roi.json")
    save_baseline(root / "baseline/empty.png", frame, roi)
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {root}\n")
    clips = ClipStore(root)

    assert main(["--config", str(config), "video-screening"]) == 0
    assert "動画がありません" in capsys.readouterr().out
    assert main(["--config", str(config), "video-screening", "--clip", "missing"]) == 2

    clip_id = clips.request()
    clips.update(
        clip_id,
        status="ready",
        path=f"clips/{clip_id}.mp4",
        scene_signature=scene_signature(frame, roi),
    )
    # 動画ファイルがなければ、その動画だけ失敗として数える。
    assert main(["--config", str(config), "video-screening"]) == 2
    assert clips.screenings.get(clip_id) is None


def test_frames_that_fail_tray_alignment_are_not_sent_to_the_detector(tmp_path):
    from support import write_video

    from poop_detector.experimental.screening import screen_clip

    path = tmp_path / "clip.mp4"
    write_video(path, np.full((76, 100, 3), 150, np.uint8), count=6)

    class Tracker:
        def align(self, frame):
            return None

    def predict(frame):
        raise AssertionError("位置合わせに失敗したコマを調べた")

    result = screen_clip(path, predict, ROI(0, 0, 1, 1), 0.4, tray_tracker=Tracker())
    assert result.unaligned == result.sampled > 0
    assert (result.tray_frames, result.weak_tray_seconds) == (0, 0)
