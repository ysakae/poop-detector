"""判定器とモデルの状態（比較の結果の読み込み、犬検出モデル、判定器ごとの判定の件数。#109）を検証する。"""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from support import ready_clip

from poop_detector.features import dog_status
from poop_detector.features.dog import MODEL_FILES
from poop_detector.video import benchmark_results
from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.clips import ClipStore


def summary(cases=3, feces=(1, 0, 0), urine=(0, 0, 1), uncertain=1, errors=0):
    """benchmark.summarize と同じ形の要約。数は (真陽性, 偽陽性, 偽陰性)。"""

    def scores(counts):
        tp, fp, fn = counts
        return {
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
        }

    return {
        "cases": cases,
        "uncertain": uncertain,
        "errors": errors,
        "types": {"feces": scores(feces), "urine": scores(urine)},
    }


def write_report(folder, name, created_at, summaries, **extra):
    folder.mkdir(parents=True, exist_ok=True)
    report = {
        "version": 1,
        "created_at": created_at,
        "dataset": "set.json",
        "cases": [{"id": "clip-secret-id"}],
        "summaries": summaries,
        **extra,
    }
    (folder / name).write_text(json.dumps(report, ensure_ascii=False))


def test_default_output_is_a_new_file_in_the_results_folder(tmp_path):
    now = datetime(2026, 11, 12, 1, 2, 3, tzinfo=UTC)
    path = benchmark_results.default_output(tmp_path, now)
    assert path.parent == tmp_path / benchmark_results.RESULTS_DIR
    assert path.name == "20261112-010203.json"


def test_results_are_read_newest_first_with_scores(tmp_path):
    folder = tmp_path / benchmark_results.RESULTS_DIR
    write_report(folder, "a.json", "2026-11-01T00:00:00+00:00", {"codex:a": summary()})
    write_report(
        folder,
        "b.json",
        "2026-11-10T00:00:00+00:00",
        {"codex:a": summary(), "claude:b": summary(feces=(0, 0, 1), errors=2)},
    )
    results, unreadable = benchmark_results.load_results(tmp_path)
    assert unreadable == 0
    assert [result.created_at for result in results] == [
        "2026-11-10T00:00:00+00:00",
        "2026-11-01T00:00:00+00:00",
    ]
    first = results[0]
    assert first.dataset == "set.json"
    assert [score.name for score in first.scores] == ["codex:a", "claude:b"]
    claude = first.scores[1]
    assert (claude.cases, claude.uncertain, claude.errors) == (3, 1, 2)
    assert claude.feces_precision is None
    assert claude.feces_recall == 0
    assert first.scores[0].feces_precision == 1


def test_broken_or_foreign_files_are_counted_not_shown(tmp_path):
    folder = tmp_path / benchmark_results.RESULTS_DIR
    write_report(folder, "ok.json", "2026-11-10T00:00:00+00:00", {"codex:a": summary()})
    (folder / "broken.json").write_text("{")
    (folder / "list.json").write_text("[]")
    write_report(folder, "future.json", "2026-11-11T00:00:00+00:00", {}, version=99)
    write_report(folder, "bad.json", "2026-11-11T00:00:00+00:00", {"codex:a": {"cases": "x"}})
    (folder / "notes.txt").write_text("JSON 以外は読まない")
    results, unreadable = benchmark_results.load_results(tmp_path)
    assert [result.scores[0].name for result in results] == ["codex:a"]
    assert unreadable == 4


def test_only_the_newest_results_are_read(tmp_path):
    folder = tmp_path / benchmark_results.RESULTS_DIR
    for day in range(1, 9):
        created = f"2026-11-0{day}T00:00:00+00:00"
        write_report(folder, f"2026110{day}.json", created, {"codex:a": summary()})
    results, _ = benchmark_results.load_results(tmp_path, limit=3)
    assert [result.created_at[:10] for result in results] == [
        "2026-11-08",
        "2026-11-07",
        "2026-11-06",
    ]


def test_missing_results_folder_is_empty(tmp_path):
    assert benchmark_results.load_results(tmp_path) == ([], 0)


def write_models(folder, *, corrupt=None, skip=None):
    """ハッシュが一致するふりをするため、MODEL_FILES を書いた中身のハッシュに差し替える。"""
    folder.mkdir(parents=True)
    hashes = {}
    for name in MODEL_FILES:
        if name == skip:
            continue
        data = f"{name} の中身".encode()
        (folder / name).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    if corrupt:
        (folder / corrupt).write_bytes(b"changed")
    return hashes


@pytest.fixture
def model_hashes(monkeypatch):
    def install(hashes):
        full = {name: hashes.get(name, "0" * 64) for name in MODEL_FILES}
        monkeypatch.setattr(dog_status, "MODEL_FILES", full)

    return install


def test_dog_model_that_matches_the_hashes_is_verified(tmp_path, model_hashes):
    folder = tmp_path / "dog-yolox"
    model_hashes(write_models(folder))
    status = dog_status.model_status(folder)
    assert (status.present, status.verified) == (True, True)
    assert status.missing == ()
    assert status.mismatched == ()


def test_dog_model_reports_missing_and_changed_files(tmp_path, model_hashes):
    names = list(MODEL_FILES)
    folder = tmp_path / "dog-yolox"
    model_hashes(write_models(folder, corrupt=names[0], skip=names[1]))
    status = dog_status.model_status(folder)
    assert (status.present, status.verified) == (False, False)
    assert status.missing == (names[1],)
    assert status.mismatched == (names[0],)
    assert dog_status.model_status(tmp_path / "none").missing == tuple(names)


def test_check_results_are_saved_without_the_image_path(tmp_path):
    folder = tmp_path / "dog-yolox"
    folder.mkdir()
    measured = {
        "dog_boxes": [[1, 2, 3, 4]],
        "mean_inference_ms": 12.5,
        "cpu_percent_inference_loop": 80.0,
        "rss_mib": 300.0,
    }
    dog_status.save_check(folder, measured, repeat=5, at="2026-11-12T00:00:00+00:00")
    saved = dog_status.load_check(folder)
    assert saved == dog_status.CheckResult(
        checked_at="2026-11-12T00:00:00+00:00",
        repeat=5,
        dogs=1,
        mean_inference_ms=12.5,
        cpu_percent=80.0,
        rss_mib=300.0,
    )
    assert "image" not in (folder / dog_status.CHECK_FILE).read_text()


def test_broken_check_result_is_ignored(tmp_path):
    folder = tmp_path / "dog-yolox"
    assert dog_status.load_check(folder) is None
    folder.mkdir()
    (folder / dog_status.CHECK_FILE).write_text('{"repeat": "x"}')
    assert dog_status.load_check(folder) is None


def test_assessments_are_counted_per_judge(tmp_path):
    clips = ClipStore(tmp_path)
    reviews = clips.assessments
    ok = ready_clip(clips)
    failed = ready_clip(clips)
    other = ready_clip(clips)
    reviews.save(ok, Assessment(label="none"), provider="codex", model=None)
    reviews.save(failed, error="判定器の実行に失敗", provider="codex", model=None)
    reviews.save(other, Assessment(label="feces"), provider="claude", model="model-b")
    usage = {(row.provider, row.model): row for row in reviews.by_judge()}
    codex = usage["codex", None]
    assert (codex.total, codex.errors) == (2, 1)
    assert codex.last_at
    assert codex.last_failed is True
    claude = usage["claude", "model-b"]
    assert (claude.total, claude.errors, claude.last_failed) == (1, 0, False)
