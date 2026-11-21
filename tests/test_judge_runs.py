"""判定器の呼び出しの記録（judge_runs）と、期間別の回数の集計を検証する。"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from support import ready_clip

from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.clips import ClipStore
from poop_detector.video.judge_runs import RecordingJudge
from poop_detector.video.judge_source import FixedJudge, JudgeChoice
from poop_detector.video.review import Review

TOKYO = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=TOKYO)


def at(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=TOKYO)


def test_runs_are_counted_by_period(tmp_path):
    runs = ClipStore(tmp_path).judge_runs
    runs.record("codex", None, "review", failed=False, at=at(2026, 11, 14, 1, 0))
    runs.record("codex", None, "review", failed=True, at=at(2026, 11, 10, 9, 0))
    runs.record("codex", None, "review", failed=False, at=at(2026, 10, 31, 20, 0))
    runs.record("codex", None, "review", failed=False, at=at(2026, 9, 1, 8, 0))
    runs.record("claude", "model-b", "benchmark", failed=False, at=at(2026, 11, 13, 8, 0))

    usage = {(row.provider, row.model, row.purpose): row for row in runs.usage(NOW)}

    codex = usage["codex", None, "review"]
    counts = (codex.today, codex.last_7_days, codex.last_30_days, codex.this_month, codex.total)
    assert counts == (1, 2, 3, 2, 4)
    assert codex.failures == 1
    assert codex.last_failed is False
    assert codex.last_at.startswith("2026-11-13T16:00")
    claude = usage["claude", "model-b", "benchmark"]
    assert (claude.today, claude.last_7_days, claude.total) == (0, 1, 1)
    # 直近に使ったものから並べる。
    assert [row.provider for row in runs.usage(NOW)] == ["codex", "claude"]


def test_unknown_purpose_is_refused(tmp_path):
    runs = ClipStore(tmp_path).judge_runs
    with pytest.raises(ValueError, match="用途"):
        runs.record("codex", None, "other", failed=False)


class Judge:
    """呼ばれた回数を数え、指定した指示で失敗する偽の判定器。"""

    name = "偽の判定器"

    def __init__(self, fail_on: str = "") -> None:
        self.fail_on = fail_on
        self.calls = 0

    def check(self) -> None:
        pass

    def judge(self, folder, prompt, images, schema):
        self.calls += 1
        if prompt == self.fail_on:
            raise TimeoutError("出力に含めない詳細")
        return {"label": "none"}


def test_recording_judge_records_success_and_failure_and_passes_results(tmp_path):
    runs = ClipStore(tmp_path).judge_runs
    inner = Judge(fail_on="失敗")
    judge = RecordingJudge(inner, runs, "claude", "model-b", "benchmark")

    assert judge.name == inner.name
    judge.check()
    assert judge.judge(tmp_path, "成功", [], {}) == {"label": "none"}
    with pytest.raises(TimeoutError):
        judge.judge(tmp_path, "失敗", [], {})

    (row,) = runs.usage()
    assert (row.provider, row.model, row.purpose) == ("claude", "model-b", "benchmark")
    assert (row.total, row.failures, row.last_failed) == (2, 1, True)


def test_review_records_each_call_of_the_judge(tmp_path):
    store = ClipStore(tmp_path)
    ok, bad, skipped = ready_clip(store), ready_clip(store), ready_clip(store)
    for clip in (ok, bad):
        path = tmp_path / "clips" / f"{clip}.mp4"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"")
    judge = Judge(fail_on=bad)

    def inspect(path, row, chosen):
        chosen.judge(path.parent, row["id"], [], {})
        return Assessment("none", "other", None, None, "排泄なし")

    review = Review(store, inspect, judges=FixedJudge(JudgeChoice(judge, "claude", "model-b")))

    # 動画ファイルがない録画は判定器を呼ばないので数えない。
    assert review.run(limit=3) == (1, 2)
    assert judge.calls == 2
    assert store.assessments.get(skipped)["error"]
    (row,) = store.judge_runs.usage()
    assert (row.provider, row.model, row.purpose) == ("claude", "model-b", "review")
    assert (row.total, row.failures) == (2, 1)


def test_recording_judge_keeps_only_the_kind_of_failure(tmp_path, caplog):
    runs = ClipStore(tmp_path).judge_runs
    judge = RecordingJudge(Judge(fail_on="失敗"), runs, "gemini", None, "review")

    judge.judge(tmp_path, "成功", [], {})
    with pytest.raises(TimeoutError):
        judge.judge(tmp_path, "失敗", [], {})

    with runs.connect() as db:
        rows = db.execute("SELECT failed, failure, failure_detail FROM judge_runs ORDER BY id")
        assert [tuple(row) for row in rows] == [(0, None, None), (1, "timeout", None)]
    # ログには種類だけを出し、例外の文言は出さない。
    assert "timeout" in caplog.text
    assert "出力に含めない詳細" not in caplog.text


def test_failures_are_counted_by_kind_since_a_time(tmp_path):
    runs = ClipStore(tmp_path).judge_runs
    runs.record("gemini", None, "review", failed=True, failure="timeout", at=at(2026, 11, 14, 9))
    runs.record("gemini", None, "review", failed=True, failure="timeout", at=at(2026, 11, 14, 10))
    runs.record(
        "gemini", None, "review", failed=True, failure="rate_limited", at=at(2026, 11, 14, 11)
    )
    # 種類を記録する前の失敗は「不明」として数える。
    runs.record("gemini", None, "review", failed=True, at=at(2026, 11, 14, 11, 30))
    runs.record("gemini", None, "review", failed=False, at=at(2026, 11, 14, 11, 40))
    runs.record("gemini", None, "review", failed=True, failure="timeout", at=at(2026, 11, 12))
    runs.record("codex", None, "benchmark", failed=True, failure="exit", at=at(2026, 11, 14, 11))

    counts = runs.failure_counts(at(2026, 11, 13), purpose="review")

    assert counts == [("timeout", 2), ("rate_limited", 1), (None, 1)]


def test_unknown_failure_kind_is_refused(tmp_path):
    runs = ClipStore(tmp_path).judge_runs
    with pytest.raises(ValueError, match="種類"):
        runs.record("codex", None, "review", failed=True, failure="secret text")


def test_trailing_failures_count_only_after_the_last_success(tmp_path):
    runs = ClipStore(tmp_path).judge_runs
    assert runs.trailing_failures() == 0
    runs.record("codex", None, "review", failed=True, at=at(2026, 11, 14, 8, 0))
    runs.record("codex", None, "review", failed=True, at=at(2026, 11, 14, 8, 1))
    # 成功がまだなければ、失敗をすべて数える。
    assert runs.trailing_failures() == 2
    runs.record("codex", None, "review", failed=False, at=at(2026, 11, 14, 8, 2))
    assert runs.trailing_failures() == 0
    # 同じ時刻の記録は、あとに書いたほうを新しいとみなす。
    runs.record("codex", None, "review", failed=True, at=at(2026, 11, 14, 8, 2))
    runs.record("codex", None, "review", failed=True, at=at(2026, 11, 14, 8, 3))
    # 別の用途（比較）の失敗と成功は数えない。
    runs.record("codex", None, "benchmark", failed=False, at=at(2026, 11, 14, 8, 4))
    assert runs.trailing_failures() == 2
    assert runs.trailing_failures("benchmark") == 0


def test_trailing_failures_stay_fast_with_many_runs(tmp_path):
    # 呼び出しの記録が増えても、行の組み合わせを数えずに求める（2万件でもすぐ終わる）。
    runs = ClipStore(tmp_path).judge_runs
    stamps = [
        f"2026-11-{1 + i // 1440:02d}T{i // 60 % 24:02d}:{i % 60:02d}:00+00:00"
        for i in range(20000)
    ]
    with runs.connect() as db:
        db.executemany(
            "INSERT INTO judge_runs (provider, model, purpose, ran_at, failed) VALUES (?,?,?,?,?)",
            [("codex", None, "review", stamp, int(i >= 19997)) for i, stamp in enumerate(stamps)],
        )
    started = time.perf_counter()
    assert runs.trailing_failures() == 3
    assert time.perf_counter() - started < 1
