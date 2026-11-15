"""判定器の CLI の出力からトークン数を取り出し、呼び出しの記録に残す処理を確認する。"""

import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta

from poop_detector.records.database import LATEST_VERSION, MIGRATIONS, Database
from poop_detector.video.clips import ClipStore
from poop_detector.video.codex_cli import CodexCliJudge
from poop_detector.video.judge_runs import JudgeRunLog, RecordingJudge
from poop_detector.video.token_usage import TokenUsage, parse_claude, parse_codex

CODEX_EVENTS = b"\n".join(
    json.dumps(event).encode()
    for event in (
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 17518,
                "cached_input_tokens": 4096,
                "cache_write_input_tokens": 0,
                "output_tokens": 215,
                "reasoning_output_tokens": 128,
            },
        },
    )
)


def test_codex_usage_comes_from_the_turn_completed_event():
    assert parse_codex(CODEX_EVENTS) == TokenUsage(17518, 4096, 215)
    assert parse_codex(b"") is None
    assert parse_codex(None) is None
    assert parse_codex(b'not json\n{"type":"turn.completed"}\n') is None
    broken = b'{"type":"turn.completed","usage":{"input_tokens":-1,"output_tokens":"many"}}'
    assert parse_codex(broken) is None
    assert parse_codex(b"x" * (2 * 1024 * 1024)) is None


def test_claude_usage_adds_cached_input_to_the_input_tokens():
    usage = {
        "input_tokens": 12,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 3000,
        "output_tokens": 310,
        "service_tier": "standard",
    }
    assert parse_claude(usage) == TokenUsage(12012, 9000, 310)
    assert parse_claude({"input_tokens": 5, "output_tokens": 7}) == TokenUsage(5, 0, 7)
    assert parse_claude(None) is None
    assert parse_claude({"input_tokens": True, "output_tokens": 7}) is None


def test_codex_judge_asks_for_json_events_and_keeps_the_usage(tmp_path):
    def run(args, **kwargs):
        assert "--json" in args
        assert kwargs["stdout"] == subprocess.PIPE
        (tmp_path / "result.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, CODEX_EVENTS)

    judge = CodexCliJudge(run)
    assert judge.judge(tmp_path, "指示", ["frames.jpg"], {}) == {}
    assert judge.last_usage == TokenUsage(17518, 4096, 215)

    def silent(args, **kwargs):
        (tmp_path / "result.json").write_text("{}", encoding="utf-8")

    quiet = CodexCliJudge(silent)
    quiet.judge(tmp_path, "指示", ["frames.jpg"], {})
    assert quiet.last_usage is None


class Judge:
    name = "偽の判定器"

    def __init__(self, usage, *, fail=False):
        self.usage, self.fail = usage, fail
        self.last_usage = None

    def check(self):
        pass

    def judge(self, folder, prompt, images, schema):
        self.last_usage = self.usage
        if self.fail:
            raise ValueError("失敗")
        return {}


def test_recorded_runs_average_the_tokens_of_measured_calls(tmp_path):
    store = ClipStore(tmp_path)
    log = store.judge_runs
    now = datetime.now(UTC)
    RecordingJudge(Judge(TokenUsage(1000, 400, 50)), log, "codex", None, "review").judge(
        tmp_path, "", [], {}
    )
    RecordingJudge(Judge(TokenUsage(3000, 0, 150)), log, "codex", None, "review").judge(
        tmp_path, "", [], {}
    )
    # トークン数を出さない判定器の呼び出しと、30日より前の呼び出しは平均に入れない。
    RecordingJudge(Judge(None), log, "codex", None, "review").judge(tmp_path, "", [], {})
    log.record(
        "codex",
        None,
        "review",
        failed=False,
        at=now - timedelta(days=40),
        usage=TokenUsage(90000, 0, 9000),
    )
    (usage,) = log.usage(now)
    assert usage.total == 4
    assert (usage.measured, usage.input_tokens, usage.cached_tokens, usage.output_tokens) == (
        2,
        4000,
        400,
        200,
    )


def test_version_16_database_gains_token_columns_and_keeps_old_runs(tmp_path):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:16]:
            migration(db)
        db.execute("PRAGMA user_version = 16")
        db.execute(
            "INSERT INTO judge_runs (provider, model, purpose, ran_at, failed)"
            " VALUES ('codex', NULL, 'review', '2026-11-01T00:00:00+00:00', 0)"
        )
    log = JudgeRunLog(Database(path))
    log.record("codex", None, "review", failed=False, usage=TokenUsage(10, 2, 3))
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
        rows = db.execute(
            "SELECT input_tokens, cached_tokens, output_tokens FROM judge_runs ORDER BY id"
        ).fetchall()
    assert rows == [(None, None, None), (10, 2, 3)]
