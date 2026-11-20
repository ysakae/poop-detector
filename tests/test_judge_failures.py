"""判定器の呼び出しの失敗を、CLI の出力を残さずに決まった種類へ分ける。"""

import json
import subprocess

import pytest

from poop_detector.video.judge_failures import (
    FAILURE_KINDS,
    FAILURE_NAMES,
    JudgeCallError,
    failure_cause,
    mentions_rate_limit,
)


@pytest.mark.parametrize(
    ("error", "kind", "detail"),
    [
        (JudgeCallError("応答がタイムアウトしました", "timeout"), "timeout", None),
        (JudgeCallError("拒否", "rejected", "code:-32603"), "rejected", "code:-32603"),
        (subprocess.TimeoutExpired(["codex"], 120), "timeout", None),
        (TimeoutError("出力に含めない詳細"), "timeout", None),
        (subprocess.CalledProcessError(3, ["codex"]), "exit", "exit:3"),
        (FileNotFoundError("codex"), "launch", None),
        (json.JSONDecodeError("詳細", "doc", 0), "invalid_output", None),
        (ValueError("出力に含めない詳細"), "other", "ValueError"),
        (KeyboardInterrupt(), "other", "KeyboardInterrupt"),
    ],
)
def test_failure_cause_maps_errors_to_fixed_kinds(error, kind, detail):
    cause = failure_cause(error)
    assert (cause.kind, cause.detail) == (kind, detail)
    assert cause.kind in FAILURE_KINDS


def test_every_kind_has_a_name_for_the_screen():
    assert set(FAILURE_NAMES) == set(FAILURE_KINDS)


def test_unknown_kind_and_free_text_detail_are_refused():
    with pytest.raises(ValueError, match="種類"):
        JudgeCallError("失敗", "secret")
    # 詳細には CLI の文言を入れられない（番号や決まった語だけ）。
    with pytest.raises(ValueError, match="詳細"):
        JudgeCallError("失敗", "rejected", "quota exceeded for user@example.com")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Resource has been exhausted (e.g. check quota).", True),
        ("RESOURCE_EXHAUSTED", True),
        ("429 Too Many Requests", True),
        ("You have reached your rate limit", True),
        ("The model is overloaded. Please try again later.", True),
        ("no capacity available for this model", True),
        ("usage limit reached", True),
        ({"code": 429}, True),
        ("invalid json", False),
        (None, False),
    ],
)
def test_rate_limit_wording_is_recognized(text, expected):
    assert mentions_rate_limit(text) is expected
