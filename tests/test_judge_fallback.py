"""判定器の呼び出しが失敗し続けたときに、予備の判定器へ切り替える処理を確認する。"""

import subprocess
from datetime import UTC, datetime

import pytest
from test_video_review import FECES, ready

from poop_detector.config import Config, ConfigError
from poop_detector.video.clips import ClipStore
from poop_detector.video.judge_source import FallbackJudges, FixedJudge, JudgeChoice
from poop_detector.video.review import Review


class Judge:
    """名前の付いた偽の判定器。down の間は呼び出しに失敗し、ready が False ならログインしていない。"""

    def __init__(self, name, *, down=False, ready=True):
        self.name, self.down, self.ready = name, down, ready
        self.calls = 0

    def check(self):
        if not self.ready:
            raise ValueError("ログインしてください")

    def judge(self, folder, prompt, images, schema):
        self.calls += 1
        if self.down:
            raise subprocess.CalledProcessError(1, "judge")
        return {}


def fallback(primary, spares, *, now):
    return FallbackJudges(
        FixedJudge(JudgeChoice(primary, "codex", "model-a")),
        tuple(spares),
        build=lambda provider, model: spares[provider],
        clock=lambda: now[0],
    )


def test_switches_to_the_spare_after_repeated_failures_and_tries_the_primary_again_later():
    now = [0.0]
    primary, spare = Judge("主"), Judge("予備")
    judges = fallback(primary, {"claude": spare}, now=now)
    judges.note(2)
    assert judges.current().judge is primary
    judges.note(3)
    choice = judges.current()
    # 予備の判定器は、その CLI の既定のモデルで使う。
    assert (choice.judge, choice.provider, choice.model) == (spare, "claude", None)
    now[0] = 3599
    assert judges.current().judge is spare
    now[0] = 3600
    assert judges.current().provider == "codex"
    # 戻した主の判定器がまだ失敗するなら、もう一度予備に切り替える。
    judges.note(3)
    assert judges.current().judge is spare


def test_skips_spares_that_are_not_ready_and_keeps_the_current_judge_when_none_is():
    now = [0.0]
    primary = Judge("主")
    offline, spare = Judge("未ログイン", ready=False), Judge("予備")
    judges = fallback(primary, {"claude": offline, "gemini": spare}, now=now)
    judges.note(3)
    assert judges.current().judge is spare

    alone = fallback(primary, {"claude": offline}, now=now)
    alone.note(3)
    assert alone.current().judge is primary


def test_moves_on_when_the_spare_fails_too_and_comes_back_around_to_the_primary():
    now = [0.0]
    primary, first, second = Judge("主"), Judge("予備1"), Judge("予備2")
    judges = fallback(primary, {"claude": first, "gemini": second}, now=now)
    judges.note(3)
    assert judges.current().judge is first
    # 切り替えた先も失敗したら、続けて失敗した回数は増え続けるので、次の判定器へ進む。
    judges.note(4)
    assert judges.current().judge is second
    judges.note(5)
    assert judges.current().provider == "codex"


def test_review_uses_the_spare_and_records_it_in_the_ledger(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    clip_id = ready(store, when)
    now = [0.0]
    primary, spare = Judge("主", down=True), Judge("予備")
    judges = fallback(primary, {"claude": spare}, now=now)

    def inspect(path, row, judge):
        judge.judge(path.parent, "", [], {})
        return FECES

    review = Review(store, inspect, judges=judges)
    for _ in range(3):
        assert review.run(limit=1, since=when.isoformat()) == (0, 1)
    assert review.run(limit=1, since=when.isoformat()) == (1, 0)
    assert (primary.calls, spare.calls) == (3, 1)
    assessment = store.assessments.get(clip_id)
    assert (assessment["label"], assessment["provider"], assessment["model"]) == (
        "feces",
        "claude",
        None,
    )


def test_fallback_setting_lists_known_judges(tmp_path):
    assert Config().review_fallback == ""
    path = tmp_path / "config.yaml"
    path.write_text("review: {provider: codex, fallback: 'claude, gemini'}\n", encoding="utf-8")
    assert Config.load(path).fallback_providers == ("claude", "gemini")
    path.write_text("review: {fallback: 'claude,unknown'}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"review.fallback"):
        Config.load(path)
    # 主の判定器と同じものは、予備に数えない。
    assert Config(review_provider="claude", review_fallback="claude,codex").fallback_providers == (
        "codex",
    )
