"""CLI のプランの使用量（5時間・週間の消費割合）の読み取りを、偽の CLI で検証する。

実際の CLI は呼ばない。出力の形は各 CLI の版（Codex CLI 0.167、Claude Code 2.1、
Antigravity CLI 1.5）で確かめたものを、識別子を伏せて写している。
"""

import io
import json
import subprocess
from datetime import UTC, datetime

import pytest

from poop_detector.video import plan_usage
from poop_detector.video.plan_usage import (
    PlanUsageBoard,
    QuotaGroup,
    QuotaWindow,
    parse_antigravity,
    parse_claude,
    parse_codex,
    read_antigravity,
    read_claude,
    read_codex,
    read_plan_usage,
)

CODEX_LIMITS = {
    "id": 2,
    "result": {
        "accountId": "account-should-not-appear",
        "rateLimits": {
            "limitId": "codex",
            "limitName": None,
            "primary": {"usedPercent": 30, "windowDurationMins": 300, "resetsAt": 1794670787},
            "secondary": {"usedPercent": 93, "windowDurationMins": 10080, "resetsAt": 1794988996},
            "planType": "plus",
        },
    },
}
CLAUDE_TEXT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "Current session: 8% used · resets Nov 15 at 2:29am (Asia/Tokyo)\n"
    "Current week (all models): 3% used · resets Nov 21 at 7:59am (Asia/Tokyo)\n"
    "Current week (Fable): 0% used · resets Nov 21 at 8am (Asia/Tokyo)\n\n"
    "Last 24h · 37 requests · 2 sessions\n  Top MCP servers: private-server 15%\n"
)
AGY_USAGE = {
    "status": "SUCCESS",
    "num_turns": 0,
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "window": "weekly",
                            "remaining_fraction": 0.75,
                            "reset_time": "2026-11-21T14:02:29Z",
                        },
                        {
                            "window": "5h",
                            "remaining_fraction": 1,
                            "reset_time": "2026-11-14T19:02:29Z",
                        },
                    ],
                },
                {
                    "name": "Gemini 4 Argon",
                    "buckets": [
                        {
                            "window": "weekly",
                            "remaining_fraction": 0,
                            "reset_time": "2026-11-20T22:51:58Z",
                        }
                    ],
                },
            ]
        },
    },
}


def test_codex_limits_are_read_by_window_length():
    (group,) = parse_codex(CODEX_LIMITS)
    assert group.name == "Codex"
    assert group.five_hour == QuotaWindow(30, datetime.fromtimestamp(1794670787, UTC))
    assert group.weekly == QuotaWindow(93, datetime.fromtimestamp(1794988996, UTC))


@pytest.mark.parametrize(
    "message",
    [
        {"id": 2, "error": {"code": -32600, "message": "not logged in"}},
        {"id": 2, "result": {"rateLimits": None}},
        {"id": 2, "result": {"rateLimits": {"primary": {"usedPercent": "30"}}}},
        {"id": 2, "result": {"rateLimits": {"primary": {"usedPercent": 130}}}},
        {"id": 2, "result": {"rateLimits": {"primary": {"usedPercent": True}}}},
    ],
)
def test_codex_limits_without_valid_numbers_are_refused(message):
    with pytest.raises(ValueError, match="Codex CLI"):
        parse_codex(message)


def test_claude_usage_text_gives_percentages_only():
    groups = parse_claude(json.dumps({"subtype": "success", "result": CLAUDE_TEXT}).encode())
    assert groups == (
        QuotaGroup("全モデル", QuotaWindow(8, None), QuotaWindow(3, None)),
        QuotaGroup("Fable", None, QuotaWindow(0, None)),
    )


@pytest.mark.parametrize(
    "output",
    [b"not json", b'{"result": 3}', json.dumps({"result": "Unknown command"}).encode()],
)
def test_claude_output_without_usage_is_refused(output):
    with pytest.raises(ValueError, match="Claude Code CLI"):
        parse_claude(output)


def test_antigravity_remaining_fraction_becomes_used_percent():
    groups = parse_antigravity(json.dumps(AGY_USAGE).encode())
    assert groups == (
        QuotaGroup(
            "Gemini Models",
            QuotaWindow(0, datetime(2026, 11, 14, 19, 2, 29, tzinfo=UTC)),
            QuotaWindow(25, datetime(2026, 11, 21, 14, 2, 29, tzinfo=UTC)),
        ),
        QuotaGroup(
            "Gemini 4 Argon", None, QuotaWindow(100, datetime(2026, 11, 20, 22, 51, 58, tzinfo=UTC))
        ),
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"command": {"name": "credits", "data": {}}},
        {
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {"name": "x", "buckets": [{"window": "5h", "remaining_fraction": 2}]}
                    ]
                },
            }
        },
        {"command": {"name": "usage", "data": {"groups": []}}},
    ],
)
def test_antigravity_output_without_usage_is_refused(payload):
    with pytest.raises(ValueError, match="Antigravity CLI"):
        parse_antigravity(json.dumps(payload).encode())


def completed(stdout: bytes, returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout)


def test_claude_is_asked_without_calling_the_model(monkeypatch):
    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://user:pass@camera")
    calls = []

    def runner(command, **options):
        calls.append((command, options))
        return completed(json.dumps({"result": CLAUDE_TEXT}).encode())

    groups = read_claude(runner=runner, which=lambda name: f"/bin/{name}")
    assert groups[0].five_hour == QuotaWindow(8, None)
    command, options = calls[0]
    assert command[:3] == ["/bin/claude", "--print", "/usage"]
    # ターン数を0にして、/usage を解釈できない版でもモデルに送らない。
    assert command[command.index("--max-turns") + 1] == "0"
    assert "POOP_RTSP_URL" not in options["env"]
    assert options["stdin"] == subprocess.DEVNULL


def test_antigravity_is_asked_with_the_usage_command(monkeypatch):
    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://user:pass@camera")
    calls = []

    def runner(command, **options):
        calls.append((command, options))
        return completed(json.dumps(AGY_USAGE).encode())

    assert read_antigravity(runner=runner, which=lambda name: f"/bin/{name}")[0].weekly
    command, options = calls[0]
    assert command[:3] == ["/bin/agy", "--print", "/usage"]
    assert "--sandbox" in command
    assert "POOP_RTSP_URL" not in options["env"]


def test_failed_cli_is_reported_without_its_output():
    def runner(command, **options):
        return completed(b'{"result": "secret"}', returncode=1)

    with pytest.raises(ValueError, match="使用量を取得できません") as error:
        read_claude(runner=runner, which=lambda name: f"/bin/{name}")
    assert "secret" not in str(error.value)


def test_missing_cli_is_reported():
    with pytest.raises(plan_usage.NotInstalledError):
        read_antigravity(runner=None, which=lambda name: None)


class FakeAppServer:
    """codex app-server の偽物。受け取った要求を記録し、決まった行を返す。"""

    def __init__(self, lines):
        self.requests = io.BytesIO()
        self.stdin = self.requests
        self.stdout = io.BytesIO(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_codex_app_server_is_asked_for_rate_limits_only(monkeypatch):
    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://user:pass@camera")
    spawned = []
    notice = {"method": "remoteControl/status/changed", "params": {}}

    def spawn(command, **options):
        server = FakeAppServer([{"id": 1, "result": {}}, notice, CODEX_LIMITS])
        server.stdin.close = lambda: None
        spawned.append((command, options, server))
        return server

    (group,) = read_codex(spawn=spawn, which=lambda name: f"/bin/{name}")
    assert group.weekly.used_percent == 93
    command, options, server = spawned[0]
    assert command == ["/bin/codex", "app-server"]
    assert "POOP_RTSP_URL" not in options["env"]
    methods = [json.loads(line).get("method") for line in server.requests.getvalue().splitlines()]
    assert methods == ["initialize", "initialized", "account/rateLimits/read"]
    assert server.killed


def test_codex_app_server_without_an_answer_is_reported():
    def spawn(command, **options):
        server = FakeAppServer([{"id": 1, "result": {}}])
        server.stdin.close = lambda: None
        return server

    with pytest.raises(ValueError, match="Codex CLI"):
        read_codex(spawn=spawn, which=lambda name: f"/bin/{name}")


def test_plan_usage_turns_failures_into_problems():
    def broken():
        raise ValueError("出力の詳細")

    def missing():
        raise plan_usage.NotInstalledError

    now = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)
    failed = read_plan_usage("claude", broken, now)
    assert (failed.groups, failed.problem) == ((), plan_usage.UNKNOWN)
    assert read_plan_usage("gemini", missing, now).problem == plan_usage.NOT_INSTALLED
    ok = read_plan_usage("codex", lambda: parse_codex(CODEX_LIMITS), now)
    assert ok.problem is None
    assert ok.checked_at == now


def test_board_asks_every_cli_and_keeps_the_last_answers():
    board = PlanUsageBoard(
        {
            "codex": lambda: parse_codex(CODEX_LIMITS),
            "claude": lambda: (_ for _ in ()).throw(ValueError("x")),
        }
    )
    assert board.snapshot() == {}
    board.refresh()
    shown = board.snapshot()
    assert list(shown) == ["codex", "claude"]
    assert shown["codex"].groups[0].five_hour.used_percent == 30
    assert shown["claude"].problem == plan_usage.UNKNOWN
