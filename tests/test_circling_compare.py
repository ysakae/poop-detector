"""旋回の規則と、軌跡の特徴量による決定木を同じ動画で比べる処理を確かめる。"""

import json
import math

import cv2
import numpy as np
import pytest

from poop_detector.detection.roi import ROI
from poop_detector.detection.scene import save_baseline, scene_signature
from poop_detector.experimental.circling import find_circling
from poop_detector.experimental.circling_compare import (
    Case,
    case_label,
    evaluate,
    metrics,
)
from poop_detector.experimental.trajectory import FEATURES, trajectory_features
from poop_detector.main import main
from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clips import ClipStore


def circle(count=13, size=20.0):
    return [
        (i / 3, 50 + 14 * math.cos(i * math.tau / 12), 38 + 14 * math.sin(i * math.tau / 12), size)
        for i in range(count)
    ]


def line(count=13, size=20.0):
    return [(i / 3, 25 + i * 3, 38, size) for i in range(count)]


def test_features_separate_a_loop_from_a_walk_and_a_stop():
    loop, walk = trajectory_features(circle()), trajectory_features(line())
    stop = trajectory_features([(i / 3, 50, 38, 20.0) for i in range(13)])
    assert set(loop) == set(FEATURES)
    # 13点の円は12歩で、向きの変化は11回（30度ずつ）。
    assert loop["net_turning"] == pytest.approx(11 * math.tau / 12)
    assert walk["net_turning"] == pytest.approx(0, abs=1e-9)
    assert walk["straightness"] == pytest.approx(1)
    assert loop["straightness"] < 0.2
    assert stop["path_length"] == 0
    assert stop["stop_seconds"] == pytest.approx(4)
    assert loop["observed_seconds"] == pytest.approx(4)


def test_features_do_not_join_steps_across_a_tracking_gap():
    # 見失った間の移動は、歩いた距離にも向きの変化にも数えない。
    jumped = [(0, 10, 10, 20.0), (1 / 3, 13, 10, 20.0), (5, 90, 60, 20.0), (16 / 3, 93, 60, 20.0)]
    features = trajectory_features(jumped)
    assert features["path_length"] == pytest.approx(6 / 20)
    assert features["located"] == 4


def test_short_or_empty_tracks_have_zero_features():
    assert trajectory_features([]) == dict.fromkeys(FEATURES, 0.0)
    assert trajectory_features([(0, 1, 1, 20.0)])["located"] == 1


def test_rule_on_samples_matches_the_existing_detector():
    assert len(find_circling(circle())) == 1
    assert find_circling(line()) == []


@pytest.mark.parametrize(
    ("label", "expected"),
    [("feces", True), ("urine", True), ("both", True), ("none", False), ("uncertain", None)],
)
def test_only_confirmed_labels_are_compared(label, expected):
    assert case_label(label) is expected


def test_metrics_report_counts_and_undefined_rates():
    assert metrics([True, True, False, False], [True, False, True, False]) == {
        "tp": 1,
        "fp": 1,
        "fn": 1,
        "tn": 1,
        "precision": 0.5,
        "recall": 0.5,
    }
    assert metrics([False], [False])["precision"] is None
    assert metrics([False], [False])["recall"] is None


def cases(days=6, per_day=4):
    """排泄ありの動画は回る、なしの動画は歩くだけの、分けやすい人工の事例。"""
    made = []
    for day in range(days):
        for index in range(per_day):
            excretion = index % 2 == 0
            samples = circle() if excretion else line()
            made.append(
                Case(
                    f"c{day}-{index}",
                    f"2026-11-{day + 1:02d}",
                    excretion,
                    trajectory_features(samples),
                    len(find_circling(samples)),
                )
            )
    return made


def test_classifier_is_evaluated_out_of_fold_by_day():
    result = evaluate(cases())
    assert result["cases"] == 24
    assert result["positives"] == 12
    assert result["days"] == 6
    assert result["rule"]["recall"] == 1.0
    assert result["classifier"]["tp"] + result["classifier"]["fn"] == 12
    assert result["classifier"]["folds"] == 5
    assert result["small_sample"] is True


def test_classifier_is_skipped_without_two_days_or_both_classes():
    one_day = evaluate(cases(days=1))
    assert one_day["classifier"] == {"skipped": "評価には2日以上の動画が必要です"}
    no_positive = evaluate([case for case in cases() if not case.excretion])
    assert "skipped" in no_positive["classifier"]
    assert no_positive["rule"]["recall"] is None


def test_cases_round_trip_through_json():
    original = cases(days=1, per_day=2)
    assert [Case.from_data(json.loads(json.dumps(case.to_data()))) for case in original] == original
    with pytest.raises(ValueError, match="形式"):
        Case.from_data({"clip_id": "c", "day": "d", "excretion": "yes", "features": {}})


def write_clip(root, clips, frame, roi, label):
    clip_id = clips.request()
    relative = f"clips/{clip_id}.mp4"
    writer = cv2.VideoWriter(str(root / relative), cv2.VideoWriter_fourcc(*"mp4v"), 3, (100, 76))
    assert writer.isOpened()
    for _ in range(13):
        writer.write(frame)
    writer.release()
    clips.update(
        clip_id,
        status="ready",
        path=relative,
        duration=13 / 3,
        start_at="2026-11-15T01:00:00+00:00",
        scene_signature=scene_signature(frame, roi),
    )
    if label is not None:
        clips.labels.save(clip_id, ClipLabel(label, activity="other"))
    return clip_id


def test_cli_compares_labeled_clips_and_reuses_saved_features(tmp_path, monkeypatch, capsys):
    root = tmp_path
    frame = np.full((76, 100, 3), 150, np.uint8)
    roi = ROI(0, 0, 1, 1)
    roi.save(root / "roi.json")
    save_baseline(root / "baseline/empty.png", frame, roi)
    clips = ClipStore(root)
    labeled = [write_clip(root, clips, frame, roi, label) for label in ("none", "uncertain", None)]
    config = root / "config.yaml"
    config.write_text(f"data_dir: {root}")

    class DogDetector:
        def __init__(self, *args):
            self.points = iter(sample[1:3] for sample in circle() * 3)

        def predict(self, _frame):
            x, y = map(round, next(self.points))
            return [(x - 10, y - 10, x + 10, y + 10)]

    monkeypatch.setattr("poop_detector.features.dog.DogDetector", DogDetector)
    output = tmp_path / "compare.json"
    assert main(["--config", str(config), "circling-compare", "--output", str(output)]) == 0
    saved = json.loads(output.read_text())
    # 人が排泄の有無を確かめた動画だけを比べる。ラベルを書き換えない。
    assert [case["clip_id"] for case in saved["cases"]] == [labeled[0]]
    assert saved["cases"][0]["day"] == "2026-11-15"
    assert saved["result"]["rule"]["fp"] == 1
    assert clips.get(labeled[0])["label"] == "none"
    printed = capsys.readouterr().out
    assert "規則" in printed

    monkeypatch.setattr(
        "poop_detector.features.dog.DogDetector",
        lambda *args: pytest.fail("保存した特徴量を使うときは犬を検出しない"),
    )
    again = tmp_path / "again.json"
    assert (
        main(
            [
                "--config",
                str(config),
                "circling-compare",
                "--features",
                str(output),
                "--output",
                str(again),
            ]
        )
        == 0
    )
    assert json.loads(again.read_text())["cases"] == saved["cases"]


def test_cli_explains_how_to_install_the_optional_dependency(tmp_path, monkeypatch, capsys):
    import builtins

    real_import = builtins.__import__

    def without_sklearn(name, *args, **kwargs):
        if name.startswith("sklearn"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_sklearn)
    features = tmp_path / "features.json"
    features.write_text(json.dumps({"cases": [case.to_data() for case in cases()]}))
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {tmp_path}")
    assert main(["--config", str(config), "circling-compare", "--features", str(features)]) == 2
    assert "uv sync --extra analysis" in capsys.readouterr().err


def _labeled_scene(root):
    frame = np.full((76, 100, 3), 150, np.uint8)
    roi = ROI(0, 0, 1, 1)
    roi.save(root / "roi.json")
    save_baseline(root / "baseline/empty.png", frame, roi)
    write_clip(root, ClipStore(root), frame, roi, "none")
    config = root / "config.yaml"
    config.write_text(f"data_dir: {root}\ndog:\n  confidence: 0.4\n")
    return config


@pytest.mark.parametrize(("given", "expected"), [([], 0.4), (["--dog-confidence", "0.25"], 0.25)])
def test_cli_measures_with_the_given_dog_confidence(tmp_path, monkeypatch, given, expected):
    config = _labeled_scene(tmp_path)
    used = []

    class DogDetector:
        def __init__(self, _directory, confidence):
            used.append(confidence)

        def predict(self, _frame):
            return []

    monkeypatch.setattr("poop_detector.features.dog.DogDetector", DogDetector)
    output = tmp_path / "compare.json"
    command = ["--config", str(config), "circling-compare", "--output", str(output), *given]
    assert main(command) == 0
    # 監視の設定は変えず、この比較の検出だけに使う。結果にも残して比べ直せるようにする。
    assert used == [expected]
    assert json.loads(output.read_text())["dog_confidence"] == expected


@pytest.mark.parametrize("value", ["0", "1", "abc"])
def test_cli_rejects_a_dog_confidence_out_of_range(tmp_path, capsys, value):
    config = _labeled_scene(tmp_path)
    with pytest.raises(SystemExit) as raised:
        main(["--config", str(config), "circling-compare", "--dog-confidence", value])
    assert raised.value.code == 2
    assert "0より大きく1未満" in capsys.readouterr().err


def test_cli_does_not_combine_dog_confidence_with_saved_features(tmp_path, capsys):
    features = tmp_path / "features.json"
    features.write_text(json.dumps({"cases": [case.to_data() for case in cases()]}))
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {tmp_path}")
    command = ["--config", str(config), "circling-compare", "--features", str(features)]
    assert main([*command, "--dog-confidence", "0.3"]) == 2
    assert "--features" in capsys.readouterr().err


def test_classifier_splits_even_with_a_dozen_clips_over_two_days():
    # 実データの序盤のように、2日で十数本でも分岐を作り、分けやすい事例を当てる。
    result = evaluate(cases(days=2, per_day=6))
    assert result["classifier"]["folds"] == 2
    assert result["classifier"]["tp"] == 6
    assert result["classifier"]["fp"] == 0
