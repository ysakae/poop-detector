"""評価セットの作成・確認・固定と、固定した版での判定器の比較（#103）を検証する。"""

import json
from datetime import date
from typing import ClassVar

import cv2
import pytest
from support import spot_scene

from poop_detector.detection.roi import ROI
from poop_detector.detection.scene import save_baseline, scene_signature
from poop_detector.errors import InvalidInputError
from poop_detector.main import main
from poop_detector.video import benchmark_sets
from poop_detector.video.benchmark_sets import build, coverage, freeze, parse
from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clip_storage import is_protected
from poop_detector.video.clips import ClipStore

CREATED = "2026-11-12T00:00:00+00:00"


def write_video(path, frame, count=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width = frame.shape[:2]
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, (width, height))
    assert writer.isOpened()
    for _ in range(count):
        writer.write(frame)
    writer.release()


def add_clip(
    clips,
    start,
    label,
    *,
    signature="scene-a",
    event_id=None,
    duration=2,
    activity="other",
    frame=None,
    location=None,
    **values,
):
    """保存済みの動画を1本登録し、人のラベルを付ける。"""
    clip_id = clips.request(event_id=event_id)
    clips.update(
        clip_id,
        status="ready",
        path=f"clips/{clip_id}.mp4",
        duration=duration,
        start_at=start,
        scene_signature=signature,
        **values,
    )
    if frame is None:
        (clips.root / "clips" / f"{clip_id}.mp4").write_bytes(b"x")
    else:
        write_video(clips.root / "clips" / f"{clip_id}.mp4", frame)
    if label is not None:
        clips.labels.save(clip_id, ClipLabel(label, activity=activity, location=location))
    return clip_id


def test_build_merges_clips_of_one_visit_and_splits_by_date(tmp_path):
    clips = ClipStore(tmp_path)
    # 同じ訪問を撮った入場と検知の録画（時間が重なる）。排泄を確認した方を残す。
    enter = add_clip(clips, "2026-11-01T01:00:00+00:00", "none", duration=40)
    detected = add_clip(clips, "2026-11-01T01:00:20+00:00", "feces", duration=30)
    sniff = add_clip(clips, "2026-11-01T05:00:00+00:00", "none", activity="sniffing")
    later = add_clip(clips, "2026-11-08T01:00:00+00:00", "urine", interrupted=1, location="outside")
    unknown = add_clip(clips, "2026-11-09T01:00:00+00:00", "uncertain")
    add_clip(clips, "2026-11-09T05:00:00+00:00", None)

    dataset = build(
        clips,
        name="eval",
        created_at=CREATED,
        scenes={"scene-a": "."},
        final_since=date(2026, 11, 8),
    )

    ids = [entry.id for entry in dataset.entries]
    assert ids == [detected, sniff, later]
    assert dataset.duplicates == ((enter, detected),)
    assert dataset.uncertain == (unknown,)
    assert dataset.unlabeled == 1
    by_id = {entry.id: entry for entry in dataset.entries}
    assert by_id[detected].split == "tuning"
    assert by_id[later].split == "final"
    assert by_id[later].tags == ("トレー外", "一部欠落")
    summary = coverage(dataset)
    assert summary["by_split"]["tuning"] == {"feces": 1, "urine": 0, "both": 0, "none": 1}
    assert summary["by_split"]["final"]["urine"] == 1
    assert summary["by_tag"]["トレー外"]["urine"] == 1
    assert summary["none_activities"] == {"sniffing": 1}


def test_clips_linked_to_the_same_event_are_duplicates(tmp_path):
    clips = ClipStore(tmp_path)
    first = add_clip(clips, "2026-11-01T01:00:00+00:00", "feces", event_id="visit")
    second = add_clip(clips, "2026-11-01T03:00:00+00:00", "feces", event_id="visit")
    dataset = build(clips, name="eval", created_at=CREATED, scenes={})
    assert len(dataset.entries) == 1
    assert {dataset.entries[0].id, *dataset.duplicates[0]} == {first, second}


def test_missed_records_without_recordings_are_counted_separately(tmp_path):
    clips = ClipStore(tmp_path)
    add_clip(clips, "2026-11-01T01:00:00+00:00", "feces", duration=30)
    clips.store.add_excretion("feces", "2026-11-01T01:00:10+00:00", missed=True)
    clips.store.add_excretion("urine", "2026-11-03T01:00:00+00:00", missed=True)
    clips.store.add_excretion("urine", "2026-11-03T02:00:00+00:00")
    dataset = build(clips, name="eval", created_at=CREATED, scenes={})
    assert coverage(dataset)["missed"] == {"total": 2, "with_recording": 1, "without_recording": 1}


def test_base_dataset_carries_tags_events_and_exclusions(tmp_path):
    clips = ClipStore(tmp_path)
    kept = add_clip(clips, "2026-11-01T01:00:00+00:00", "feces")
    dropped = add_clip(clips, "2026-11-02T01:00:00+00:00", "none")
    base = {
        "clips": [
            {"id": kept, "event": "朝の排便", "tags": ["夜間"]},
            {"id": dropped, "exclude": True},
        ]
    }
    dataset = build(clips, name="eval", created_at=CREATED, scenes={}, base=base)
    by_id = {entry.id: entry for entry in dataset.entries}
    assert by_id[kept].event == "朝の排便"
    assert by_id[kept].tags == ("夜間",)
    assert by_id[dropped].exclude
    frozen = freeze(dataset, 1)
    assert [entry.id for entry in frozen.entries] == [kept]
    with pytest.raises(InvalidInputError):
        build(clips, name="eval", created_at=CREATED, scenes={}, base={"clips": [{"id": 3}]})


def test_frozen_dataset_rejects_edits_and_malformed_files(tmp_path):
    clips = ClipStore(tmp_path)
    add_clip(clips, "2026-11-01T01:00:00+00:00", "feces")
    frozen = freeze(build(clips, name="eval", created_at=CREATED, scenes={}), 1).as_json()
    assert parse(frozen).version == 1
    edited = json.loads(json.dumps(frozen))
    edited["clips"][0]["label"] = "urine"
    with pytest.raises(InvalidInputError, match="内容が変わっています"):
        parse(edited)
    with pytest.raises(InvalidInputError, match="形式"):
        parse({"format": 2, "clips": "x"})
    with pytest.raises(InvalidInputError):
        freeze(build(ClipStore(tmp_path / "empty"), name="e", created_at=CREATED, scenes={}), 1)


def test_inspect_reports_missing_videos_changed_labels_and_scenes(tmp_path):
    clips = ClipStore(tmp_path)
    gone = add_clip(clips, "2026-11-01T01:00:00+00:00", "feces")
    relabeled = add_clip(clips, "2026-11-02T01:00:00+00:00", "urine")
    other = add_clip(clips, "2026-11-03T01:00:00+00:00", "none", signature="scene-b")
    dataset = freeze(build(clips, name="eval", created_at=CREATED, scenes={}), 1)
    (tmp_path / "clips" / f"{gone}.mp4").unlink()
    clips.labels.save(relabeled, ClipLabel("none"))

    problems = benchmark_sets.inspect(dataset, clips, {"scene-a": "scene-a"})

    assert problems.missing_videos == (gone,)
    assert problems.changed_labels == (relabeled,)
    assert problems.missing_scenes == ("scene-b",)
    assert other in problems.unprotected
    assert problems.blocking
    assert "録画が見つからない: 1件" in problems.lines()


def save_scene(folder, baseline, roi):
    roi.save(folder / "roi.json")
    save_baseline(folder / "baseline" / "empty.png", baseline, roi)
    return scene_signature(baseline, roi)


@pytest.fixture
def two_scenes(tmp_path):
    """現在の画角（data/）と、過去の画角（old-scene/）で撮った録画がある台帳。"""
    root = tmp_path / "data"
    clips = ClipStore(root)
    baseline, changed = spot_scene()
    current = save_scene(root, baseline, ROI(0, 0, 1, 1))
    old_roi = ROI(0.1, 0.1, 0.8, 0.8)
    old = save_scene(tmp_path / "old-scene", old_roi.crop(changed).copy(), old_roi)
    first = add_clip(clips, "2026-11-01T01:00:00+00:00", "none", signature=current, frame=baseline)
    second = add_clip(clips, "2026-11-08T01:00:00+00:00", "feces", signature=old, frame=changed)
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {root}\n")
    return {
        "root": root,
        "clips": clips,
        "config": str(config),
        "ids": (first, second),
        "signatures": (current, old),
        "old": tmp_path / "old-scene",
    }


def run(setup, *arguments):
    return main(["--config", setup["config"], *arguments])


def test_cli_creates_checks_and_freezes_a_dataset(two_scenes, capsys):
    setup = two_scenes
    draft = setup["root"] / "benchmarks" / "datasets" / "eval-draft.json"
    assert (
        run(
            setup,
            "benchmark-dataset",
            "create",
            "--name",
            "eval",
            "--final-since",
            "2026-11-08",
            "--scene",
            str(setup["old"]),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "調整用" in out
    assert "最終評価用" in out
    report = json.loads(draft.read_text())
    assert report["version"] is None
    assert set(report["scenes"]) == set(setup["signatures"])
    # 既存のファイルは上書きしない。
    assert run(setup, "benchmark-dataset", "create", "--name", "eval") == 2

    assert run(setup, "benchmark-dataset", "check", str(draft)) == 0
    assert "自動整理から保護していない: 2件" in capsys.readouterr().out

    assert run(setup, "benchmark-dataset", "freeze", str(draft), "--version", "1") == 0
    frozen = draft.with_name("eval-v1.json")
    assert json.loads(frozen.read_text())["version"] == 1
    assert all(is_protected(setup["clips"], clip_id) for clip_id in setup["ids"])
    assert run(setup, "benchmark-dataset", "check", str(frozen)) == 0
    assert "問題はありません" in capsys.readouterr().out


def test_freeze_refuses_missing_videos(two_scenes):
    setup = two_scenes
    assert run(setup, "benchmark-dataset", "create", "--name", "eval") == 0
    draft = setup["root"] / "benchmarks" / "datasets" / "eval-draft.json"
    (setup["root"] / "clips" / f"{setup['ids'][0]}.mp4").unlink()
    assert run(setup, "benchmark-dataset", "freeze", str(draft), "--version", "1") == 2
    assert not draft.with_name("eval-v1.json").exists()


class FakeJudge:
    """判定器の偽物。受け取った画像と指示を記録し、claude だけは失敗する。"""

    name = "テスト用"
    calls: ClassVar[list] = []

    def __init__(self, provider, model):
        self.provider, self.model = provider, model

    def check(self):
        pass

    def judge(self, folder, prompt, images, schema):
        FakeJudge.calls.append(
            (self.provider, prompt, [(folder / name).read_bytes() for name in images])
        )
        if self.provider == "claude":
            raise TimeoutError("出力に含めない詳細")
        if self.provider == "gemini":
            return {
                "label": "uncertain",
                "activity": "other",
                "start_seconds": None,
                "end_seconds": None,
                "reason": "判別できません",
                "evidence": "none",
            }
        return {
            "label": "none",
            "activity": "other",
            "start_seconds": None,
            "end_seconds": None,
            "reason": "排泄なし",
            "evidence": "none",
        }


def test_benchmark_runs_a_frozen_dataset_with_each_scene(two_scenes, monkeypatch, capsys):
    setup = two_scenes
    FakeJudge.calls = []
    monkeypatch.setattr("poop_detector.video.providers.build_judge", FakeJudge)
    monkeypatch.setattr(
        "poop_detector.commands.benchmark.cli_version",
        lambda provider: f"{provider} 9.9",
    )
    assert (
        run(
            setup,
            "benchmark-dataset",
            "create",
            "--name",
            "eval",
            "--final-since",
            "2026-11-08",
            "--scene",
            str(setup["old"]),
        )
        == 0
    )
    draft = setup["root"] / "benchmarks" / "datasets" / "eval-draft.json"
    assert run(setup, "benchmark-dataset", "freeze", str(draft), "--version", "1") == 0
    frozen = draft.with_name("eval-v1.json")
    labels_before = [setup["clips"].get(clip)["label"] for clip in setup["ids"]]
    output = setup["root"] / "report.json"
    command = [
        "video-benchmark",
        "--dataset",
        str(frozen),
        "--judge",
        "codex:model-a",
        "--judge",
        "claude:model-b",
        "--judge",
        "gemini:model-c",
        "--output",
        str(output),
        "--send-images",
    ]
    # 下書きのままでは比較しない。
    assert run(setup, *[str(draft) if part == str(frozen) else part for part in command]) == 2
    assert not FakeJudge.calls
    assert run(setup, *command) == 0

    # 3判定器に、録画ごとに同じ画像と指示を渡す。画角ごとに当時の範囲で切り出す。
    for index in range(2):
        batch = FakeJudge.calls[index * 3 : index * 3 + 3]
        assert [call[0] for call in batch] == ["codex", "claude", "gemini"]
        assert batch[0][1:] == batch[1][1:] == batch[2][1:]
    assert FakeJudge.calls[0][2] != FakeJudge.calls[3][2]
    report = json.loads(output.read_text())
    assert report["version"] == 2
    assert report["dataset_info"]["version"] == 1
    assert (
        report["dataset_info"]["content_sha256"] == json.loads(frozen.read_text())["content_sha256"]
    )
    assert report["judges"]["claude:model-b"] == {
        "provider": "claude",
        "model": "model-b",
        "cli_version": "claude 9.9",
    }
    case = report["cases"][1]
    assert case["split"] == "final"
    assert case["scene_signature"] == setup["signatures"][1]
    assert case["inputs"]["extracted_at"]
    assert case["results"]["claude:model-b"]["label"] == "error"
    summaries = report["summaries"]
    assert summaries["claude:model-b"]["errors"] == 2
    assert summaries["gemini:model-c"]["uncertain"] == 2
    assert summaries["codex:model-a"]["by_split"]["final"]["types"]["feces"]["recall"] == 0
    assert summaries["codex:model-a"]["confusion"]["feces"]["none"] == 1
    assert "出力に含めない詳細" not in output.read_text()
    # 運用のラベル・自動判定・確定記録は変えない。
    assert [setup["clips"].get(clip)["label"] for clip in setup["ids"]] == labels_before
    assert all(setup["clips"].assessments.get(clip) is None for clip in setup["ids"])
    assert setup["clips"].store.records() == []
    assert "codex:model-a" in capsys.readouterr().out


def test_benchmark_stops_before_running_when_a_scene_is_missing(two_scenes, monkeypatch, capsys):
    setup = two_scenes
    FakeJudge.calls = []
    monkeypatch.setattr("poop_detector.video.providers.build_judge", FakeJudge)
    assert run(setup, "benchmark-dataset", "create", "--name", "eval") == 0
    draft = setup["root"] / "benchmarks" / "datasets" / "eval-draft.json"
    # 過去の画角の場所を渡していないので、固定も比較もできない。
    assert run(setup, "benchmark-dataset", "freeze", str(draft), "--version", "1") == 2
    assert "ROI・基準画像" in capsys.readouterr().err


def test_cli_version_is_read_without_failing(monkeypatch):
    from poop_detector.video import cli_versions

    monkeypatch.setattr(cli_versions.shutil, "which", lambda name: None)
    assert cli_versions.cli_version("codex") is None
    assert cli_versions.cli_version("unknown") is None

    class Result:
        returncode = 0
        stdout = b"codex-cli 1.2.3\nextra\n"

    monkeypatch.setattr(cli_versions.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(cli_versions.subprocess, "run", lambda *a, **k: Result())
    assert cli_versions.cli_version("codex") == "codex-cli 1.2.3"
    Result.stdout = b"\x00" * 300
    assert cli_versions.cli_version("claude") is None


def test_load_datasets_lists_saved_sets_and_counts_unreadable(tmp_path):
    clips = ClipStore(tmp_path)
    add_clip(clips, "2026-11-01T01:00:00+00:00", "feces")
    folder = benchmark_sets.datasets_dir(tmp_path)
    benchmark_sets.write_new(
        folder / "a.json", build(clips, name="eval", created_at=CREATED, scenes={})
    )
    (folder / "broken.json").write_text("{")
    found, unreadable = benchmark_sets.load_datasets(tmp_path)
    assert [name for name, _ in found] == ["a.json"]
    assert unreadable == 1
    assert benchmark_sets.load_datasets(tmp_path / "none") == ([], 0)


@pytest.mark.parametrize(
    "arguments",
    [
        ["create", "--name", "bad name"],
        ["create", "--name", "eval", "--final-since", "11/08"],
        ["create", "--name", "eval", "--scene", "no-such-scene"],
        ["create", "--name", "eval", "--base", "no-such.json"],
        ["check"],
        ["freeze", "x.json"],
    ],
)
def test_cli_rejects_invalid_dataset_options(two_scenes, arguments):
    assert run(two_scenes, "benchmark-dataset", *arguments) == 2


def test_past_tray_registrations_are_found_without_scene_option(two_scenes):
    setup = two_scenes
    archive = setup["root"] / "tray-scenes" / "0123"
    archive.parent.mkdir()
    setup["old"].rename(archive)
    assert run(setup, "benchmark-dataset", "create", "--name", "eval") == 0
    draft = setup["root"] / "benchmarks" / "datasets" / "eval-draft.json"
    scenes = json.loads(draft.read_text())["scenes"]
    assert scenes[setup["signatures"][1]] == "tray-scenes/0123"
    assert scenes[setup["signatures"][0]] == "."
    frozen = draft.with_name("eval-v1.json")
    assert run(setup, "benchmark-dataset", "freeze", str(draft), "--version", "1") == 0
    # 固定した評価セットでは --scene を使わない。
    command = ["video-benchmark", "--dataset", str(frozen), "--judge", "codex:a"]
    assert run(setup, *command, "--judge", "claude:b", "--scene", "x", "--send-images") == 2
