"""同じラベル付き録画で複数の判定器を比べる処理を検証する。"""

import json

import cv2
import pytest
from support import ready_clip, spot_scene

from poop_detector.detection.roi import ROI
from poop_detector.detection.scene import save_baseline, scene_signature
from poop_detector.main import main
from poop_detector.video.benchmark import read_cases, summarize
from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clips import ClipStore


def test_benchmark_counts_both_abstentions_and_errors():
    result = summarize(
        [("both", "urine"), ("feces", "uncertain"), ("urine", "error"), ("none", "feces")]
    )
    assert result["cases"] == 4
    assert result["uncertain"] == 1
    assert result["errors"] == 1
    assert result["confusion"]["both"]["urine"] == 1
    assert result["types"]["feces"] == {
        "true_positive": 0,
        "false_positive": 1,
        "false_negative": 2,
        "precision": 0.0,
        "recall": 0.0,
    }
    assert result["types"]["urine"]["precision"] == 1
    assert result["types"]["urine"]["recall"] == 0.5


def test_benchmark_cli_uses_identical_inputs_and_does_not_change_labels(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "data"
    clips = ClipStore(root)
    baseline, _ = spot_scene()
    roi = ROI(0, 0, 1, 1)
    roi.save(root / "roi.json")
    save_baseline(root / "baseline" / "empty.png", baseline, roi)
    clip_id = ready_clip(clips)
    clips.update(clip_id, duration=2, scene_signature=scene_signature(baseline, roi))
    clips.labels.save(clip_id, ClipLabel("none", activity="feeding"))
    row = clips.get(clip_id)
    path = root / row["path"]
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, (200, 200))
    assert writer.isOpened()
    for _ in range(10):
        writer.write(baseline)
    writer.release()

    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {root}\n")
    manifest = tmp_path / "set.json"
    manifest.write_text(json.dumps({"clips": [{"id": clip_id, "tags": ["夜間"]}]}))
    report = tmp_path / "report.json"
    calls = []

    class Judge:
        name = "テスト用"

        def __init__(self, provider):
            self.provider = provider

        def check(self):
            pass

        def judge(self, folder, prompt, images, schema):
            calls.append((prompt, [(folder / name).read_bytes() for name in images], schema))
            if self.provider == "claude":
                raise TimeoutError("出力に含めない詳細")
            return {
                "label": "none",
                "activity": "feeding",
                "start_seconds": None,
                "end_seconds": None,
                "reason": "排泄なし",
                "evidence": "none",
            }

    monkeypatch.setattr(
        "poop_detector.video.providers.build_judge", lambda provider, model: Judge(provider)
    )
    command = [
        "--config",
        str(config),
        "video-benchmark",
        "--dataset",
        str(manifest),
        "--judge",
        "codex:model-a",
        "--judge",
        "claude:model-b",
        "--output",
        str(report),
    ]
    assert main(command) == 2
    assert not calls
    assert main([*command, "--send-images"]) == 0
    assert len(calls) == 2
    assert calls[0] == calls[1]
    payload = json.loads(report.read_text())
    assert payload["cases"][0]["label"] == "none"
    assert payload["summaries"]["codex:model-a"]["by_tag"]["夜間"]["confusion"]["none"]["none"] == 1
    assert payload["summaries"]["claude:model-b"]["errors"] == 1
    assert payload["cases"][0]["results"]["claude:model-b"]["label"] == "error"
    assert "出力に含めない詳細" not in report.read_text()
    assert "codex:model-a" in capsys.readouterr().out
    assert clips.get(clip_id)["label"] == "none"
    assert clips.assessments.get(clip_id) is None
    # --output を省くと、管理 › 判定器とモデルが読む場所に新しいファイルで保存する。
    shown = [part for part in command if part not in ("--output", str(report))]
    assert main([*shown, "--send-images"]) == 0
    (saved,) = (root / "benchmarks").glob("*.json")
    assert json.loads(saved.read_text())["summaries"]
    # 比較で判定器を呼んだ回数も、用途を分けて数える（運用の判定の台帳は変えない）。
    usage = {(row.provider, row.model): row for row in clips.judge_runs.usage()}
    assert {row.purpose for row in usage.values()} == {"benchmark"}
    assert (usage["codex", "model-a"].total, usage["codex", "model-a"].failures) == (2, 0)
    assert (usage["claude", "model-b"].total, usage["claude", "model-b"].failures) == (2, 2)


def test_benchmark_cli_requires_distinct_models_and_preserves_existing_report(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {tmp_path / 'data'}\n")
    output = tmp_path / "existing.json"
    output.write_text("元のファイル")
    command = [
        "--config",
        str(config),
        "video-benchmark",
        "--dataset",
        str(tmp_path / "set.json"),
        "--judge",
        "codex:model-a",
        "--judge",
        "codex:model-a",
        "--output",
        str(output),
        "--send-images",
    ]
    assert main(command) == 2
    assert output.read_text() == "元のファイル"


def test_dataset_excludes_unconfirmed_labels_and_rejects_duplicate_event(tmp_path):
    store = ClipStore(tmp_path)
    ids = [ready_clip(store) for _ in range(3)]
    for clip_id in ids:
        store.update(clip_id, scene_signature="scene")
        (tmp_path / "clips" / f"{clip_id}.mp4").touch()
    for clip_id in ids[1:]:
        store.labels.save(clip_id, ClipLabel("urine"))
    entries = [{"id": clip_id, "event": "same-visit"} for clip_id in ids]
    cases, excluded = read_cases({"clips": entries[:2]}, store, "scene")
    assert excluded == ids[:1]
    assert cases[0].row["id"] == ids[1]
    with pytest.raises(ValueError, match="重複"):
        read_cases({"clips": entries}, store, "scene")
