"""外部 CLI に切り出し画像を渡し、運用環境からの秘密と失敗を適切に扱う。"""

import fcntl
import json
import os
import subprocess
import sys
import time

import pytest

from poop_detector.commands.context import CommandError
from poop_detector.commands.review import ready_judge
from poop_detector.config import Config
from poop_detector.features.advice import jobs
from poop_detector.video.clips import ClipStore
from poop_detector.video.codex_cli import CodexCliJudge
from poop_detector.video.judge_failures import JudgeCallError, failure_cause
from poop_detector.video.judge_runs import RecordingJudge
from poop_detector.video.providers import (
    LOOKUP_TIMEOUT,
    ClaudeCodeJudge,
    build_judge,
    supports_lookup,
)
from poop_detector.video.token_usage import TokenUsage

SCHEMA = {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}
RESULT = {"label": "none"}


def test_claude_cli_passes_two_images_and_schema_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-key")
    monkeypatch.setenv("POOP_RTSP_URL", "private-url")
    executable = tmp_path / "claude-bin"
    executable.touch()
    launcher = tmp_path / "claude"
    launcher.symlink_to(executable)
    monkeypatch.setattr("shutil.which", lambda _: str(launcher))
    (tmp_path / "frames.jpg").write_bytes(b"frames")
    (tmp_path / "differences.jpg").write_bytes(b"differences")

    def run(args, **kwargs):
        assert args[0] == str(executable.resolve())
        assert {"ANTHROPIC_API_KEY", "POOP_RTSP_URL", "CODEX_HOME"}.isdisjoint(kwargs["env"])
        if args[1:] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, b"")
        assert "--model=sonnet" in args
        assert args[args.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in args
        assert args[args.index("--tools") + 1] == ""
        # 通常の判定ではウェブ用の許可を付けない（付くのは lookup だけ）。
        assert "--allowedTools" not in args
        assert "--no-session-persistence" in args
        assert "--safe-mode" in args
        assert json.loads(args[args.index("--json-schema") + 1]) == SCHEMA
        assert kwargs["cwd"] == tmp_path
        content = json.loads(kwargs["input"])["message"]["content"]
        assert [item["source"]["data"] for item in content[:2]] == [
            "ZnJhbWVz",
            "ZGlmZmVyZW5jZXM=",
        ]
        assert content[-1] == {"type": "text", "text": "指示"}
        response = {
            "type": "result",
            "subtype": "success",
            "structured_output": RESULT,
            "usage": {"input_tokens": 20, "cache_read_input_tokens": 5000, "output_tokens": 90},
        }
        stdout = b'{"type":"system","subtype":"init"}\n' + json.dumps(response).encode() + b"\n"
        return subprocess.CompletedProcess(args, 0, stdout)

    monkeypatch.setattr("subprocess.run", run)
    judge = ready_judge(Config(review_provider="claude", review_model="sonnet"))
    assert judge.name == "Claude Code CLI"
    assert judge.judge(tmp_path, "指示", ["frames.jpg", "differences.jpg"], SCHEMA) == RESULT
    # 結果の usage から、キャッシュの分を含む入力と出力のトークン数を残す。
    assert judge.last_usage == TokenUsage(5020, 5000, 90)


@pytest.mark.parametrize(("provider", "command"), [("claude", "claude"), ("gemini", "agy")])
def test_selected_cli_reports_missing_executable(monkeypatch, provider, command):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(CommandError, match=command):
        ready_judge(Config(review_provider=provider))


@pytest.mark.parametrize(
    "output",
    [
        b"non-json private-key",
        b'{"type":"result","subtype":"error_during_execution","result":"private-key"}',
        b'{"type":"result","subtype":"success","structured_output":[]}',
    ],
)
def test_claude_rejects_invalid_results_without_exposing_cli_output(tmp_path, monkeypatch, output):
    monkeypatch.setattr("shutil.which", lambda _: "/bin/claude")
    monkeypatch.setattr(
        "subprocess.run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, output)
    )
    judge = build_judge("claude")
    with pytest.raises(ValueError, match="有効な判定") as error:
        judge.judge(tmp_path, "指示", [], SCHEMA)
    assert "private-key" not in str(error.value)


@pytest.mark.parametrize(
    ("scenario", "expected_error", "cause"),
    [
        ("ok", None, None),
        ("crash", "応答がありません", ("connection", "eof")),
        ("timeout_initialize", "タイムアウト", ("timeout", "acp:initialize")),
        ("timeout_new", "タイムアウト", ("timeout", "acp:session_new")),
        ("timeout_set_mode", "タイムアウト", ("timeout", "acp:session_set_mode")),
        ("timeout_set_model", "タイムアウト", ("timeout", "acp:session_set_model")),
        ("timeout_prompt", "タイムアウト", ("timeout", "acp:session_prompt")),
        ("timeout_close", "タイムアウト", ("timeout", "acp:session_close")),
        ("timeout_quota", "タイムアウト", ("rate_limited", "timeout:acp:session_prompt")),
        ("auth", "agy acp --login", None),
        ("model", "モデル", None),
        ("tool", "ツール", ("tool_request", None)),
        ("invalid", "有効な判定", ("invalid_output", None)),
        ("refusal", "有効な判定", ("rejected", "stop:refusal")),
        ("quota", "有効な判定", ("rate_limited", "code:-32603")),
        ("error", "有効な判定", ("rejected", "code:-32603")),
        ("quota_text", "有効な判定", ("rate_limited", "invalid_output")),
        ("stderr_quota", "有効な判定", ("rate_limited", "invalid_output")),
    ],
)
def test_antigravity_cli_exchanges_acp_messages(
    tmp_path, monkeypatch, caplog, scenario, expected_error, cause
):
    executable = tmp_path / "agy"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        f"scenario = {scenario!r}\n"
        "assert sys.argv[1:4] == ['acp', '--sandbox', '--include-default-customizations=false']\n"
        "assert sys.argv[4] == '--gemini-dir' and len(sys.argv) == 6\n"
        "data_dir = Path(sys.argv[5])\n"
        "assert data_dir.is_dir() and data_dir.parent == Path.cwd().parent\n"
        "assert data_dir != Path.cwd() and data_dir != Path.home() / '.gemini'\n"
        "assert data_dir.parent.parent.name == f'poop-detector-agy-{os.getuid()}'\n"
        "assert not any(data_dir.iterdir())\n"
        "assert 'GEMINI_API_KEY' not in os.environ and 'POOP_RTSP_URL' not in os.environ\n"
        "assert not (Path.cwd() / 'frames.jpg').exists()\n"
        "def send(**message):\n"
        "    print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    method, params = request['method'], request['params']\n"
        "    if scenario == 'timeout_' + method.removeprefix('session/'):\n"
        "        time.sleep(10)\n"
        "    if scenario == 'timeout_quota' and method == 'session/prompt':\n"
        "        print('429 RESOURCE_EXHAUSTED private-key', file=sys.stderr, flush=True)\n"
        "        time.sleep(10)\n"
        "    if method == 'initialize':\n"
        "        assert params == {'protocolVersion': 1, 'clientCapabilities': {}}\n"
        "        result = {'protocolVersion': 1, 'agentCapabilities': "
        "{'promptCapabilities': {'image': True}}}\n"
        "    elif method == 'session/new':\n"
        "        assert params == {'cwd': str(Path.cwd()), 'mcpServers': []}\n"
        "        (data_dir / 'session.db').write_bytes(b'test')\n"
        "        if scenario == 'auth':\n"
        "            send(id=request['id'], error={'code': -32000})\n"
        "            break\n"
        "        result = {'sessionId': 'test'}\n"
        "    elif method == 'session/set_mode':\n"
        "        assert params == {'sessionId': 'test', 'modeId': 'plan'}\n"
        "        result = {}\n"
        "    elif method == 'session/set_model':\n"
        "        assert params == {'sessionId': 'test', 'modelId': 'gemini-test'}\n"
        "        if scenario == 'model':\n"
        "            send(id=request['id'], error={'code': -32602})\n"
        "            break\n"
        "        result = {}\n"
        "    elif method == 'session/prompt':\n"
        "        assert params['sessionId'] == 'test'\n"
        "        prompt, image = params['prompt']\n"
        "        assert prompt['type'] == 'text' and '指示' in prompt['text']\n"
        "        assert image == {'type': 'image', 'mimeType': 'image/jpeg', 'data': 'ZnJhbWVz'}\n"
        "        if scenario == 'crash':\n"
        "            sys.exit(1)\n"
        "        if scenario in ('quota', 'error'):\n"
        "            message = 'Resource exhausted: check quota private-key' "
        "if scenario == 'quota' else 'internal private-key'\n"
        "            send(id=request['id'], error={'code': -32603, 'message': message})\n"
        "            continue\n"
        "        if scenario == 'stderr_quota':\n"
        "            print('429 RESOURCE_EXHAUSTED private-key', file=sys.stderr, flush=True)\n"
        "        if scenario == 'tool':\n"
        "            send(method='session/update', params={'sessionId': 'test', "
        "'update': {'sessionUpdate': 'tool_call'}})\n"
        "        else:\n"
        "            chunks = {'ok': ('{\"label\":', '\"none\"}'), "
        "'quota_text': ('You have exhausted your quota. private-key',)}\n"
        "            for chunk in chunks.get(scenario, ('invalid',)):\n"
        "                send(method='session/update', params={'sessionId': 'test', 'update': {"
        "'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': chunk}}})\n"
        "        result = {'stopReason': 'refusal' if scenario == 'refusal' else 'end_turn'}\n"
        "    elif method == 'session/close':\n"
        "        result = {}\n"
        "    else:\n"
        "        raise AssertionError(method)\n"
        "    send(id=request['id'], result=result)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setattr("shutil.which", lambda _: str(executable))
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr("poop_detector.video.providers.JUDGE_TIMEOUT", 1)
    monkeypatch.setenv("GEMINI_API_KEY", "private-key")
    monkeypatch.setenv("POOP_RTSP_URL", "private-url")
    (tmp_path / "frames.jpg").write_bytes(b"frames")
    if scenario in ("auth", "model"):
        with pytest.raises(CommandError, match=expected_error):
            ready_judge(Config(review_provider="gemini", review_model="gemini-test"))
        assert not list(tmp_path.glob("poop-detector-agy-*/poop-agy-*"))
        return
    timeout = scenario.startswith("timeout_")
    store = ClipStore(tmp_path)
    judge = (
        RecordingJudge(
            build_judge("gemini", "gemini-test"),
            store.judge_runs,
            "gemini",
            "gemini-test",
            "review",
        )
        if timeout
        else ready_judge(Config(review_provider="gemini", review_model="gemini-test"))
    )
    assert judge.name == "Antigravity CLI"
    assert not list(tmp_path.glob("poop-detector-agy-*/poop-agy-*"))
    if expected_error:
        with pytest.raises(ValueError, match=expected_error) as error:
            judge.judge(tmp_path, "指示", ["frames.jpg"], SCHEMA)
        found = failure_cause(error.value)
        if timeout:
            assert found.detail is not None
            phase, _, elapsed = found.detail.rpartition(":")
            assert (found.kind, phase) == cause
            assert elapsed.endswith("ms")
            assert int(elapsed.removesuffix("ms")) > 0
            with store.connect() as db:
                (saved,) = db.execute("SELECT failure, failure_detail FROM judge_runs").fetchall()
            assert tuple(saved) == (found.kind, found.detail)
            assert found.detail in caplog.text
            assert "private-key" not in caplog.text
        else:
            assert (found.kind, found.detail) == cause
        assert "private-key" not in str(error.value)
    else:
        assert judge.judge(tmp_path, "指示", ["frames.jpg"], SCHEMA) == RESULT
    assert not list(tmp_path.glob("poop-detector-agy-*/poop-agy-*"))


def test_antigravity_reclaims_only_abandoned_workspaces(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/false")
    pool = tmp_path / f"poop-detector-agy-{os.getuid()}"
    pool.mkdir(mode=0o700)
    abandoned, active, recent, unrelated = (
        pool / name for name in ("poop-agy-abandoned", "poop-agy-active", "poop-agy-new", "other")
    )
    for folder in (abandoned, active, recent, unrelated):
        folder.mkdir()
        (folder / "session.db").write_bytes(b"test")
    older = time.time() - 2 * 86400
    for folder in (abandoned, active, unrelated):
        os.utime(folder, (older, older))
    link = pool / "poop-agy-link"
    link.symlink_to(unrelated, target_is_directory=True)

    judge = build_judge("gemini")
    active_fd = os.open(active, os.O_RDONLY)
    try:
        fcntl.flock(active_fd, fcntl.LOCK_EX)
        with pytest.raises(JudgeCallError):
            judge.check()
        assert not abandoned.exists()
        assert (active / "session.db").is_file()
    finally:
        os.close(active_fd)

    with pytest.raises(JudgeCallError):
        judge.check()
    assert not active.exists()
    assert (recent / "session.db").is_file()
    assert (unrelated / "session.db").is_file()
    assert link.is_symlink()


def test_cli_environment_keeps_the_login_name_for_the_keychain(monkeypatch):
    """macOS の Claude Code はログイン情報をキーチェーンに置き、USER がないと未ログインと答える。"""
    from poop_detector.video.providers import _cli_environment

    monkeypatch.setenv("USER", "dog-owner")
    monkeypatch.setenv("LOGNAME", "dog-owner")
    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://user:secret@192.0.2.10/stream")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    environment = _cli_environment()
    assert (environment["USER"], environment["LOGNAME"]) == ("dog-owner", "dog-owner")
    # 監視の接続情報と API キーは渡さない。
    assert "POOP_RTSP_URL" not in environment
    assert "ANTHROPIC_API_KEY" not in environment


@pytest.mark.parametrize(
    ("outcome", "cause"),
    [
        (subprocess.TimeoutExpired(["claude"], 120), ("timeout", None)),
        (OSError("起動できない"), ("launch", None)),
        (subprocess.CompletedProcess([], 2, b""), ("exit", "exit:2")),
        (subprocess.CompletedProcess([], 0, b"non-json private-key"), ("invalid_output", None)),
        (
            subprocess.CompletedProcess(
                [],
                0,
                b'{"type":"result","subtype":"success","is_error":true,'
                b'"result":"Claude AI usage limit reached private-key"}',
            ),
            ("rate_limited", "rejected"),
        ),
        (
            subprocess.CompletedProcess(
                [], 0, b'{"type":"result","subtype":"error_max_turns","result":"private-key"}'
            ),
            ("rejected", None),
        ),
    ],
)
def test_claude_failures_keep_their_kind(tmp_path, monkeypatch, outcome, cause):
    monkeypatch.setattr("shutil.which", lambda _: "/bin/claude")

    def run(args, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("subprocess.run", run)
    with pytest.raises(JudgeCallError) as error:
        build_judge("claude").judge(tmp_path, "指示", [], SCHEMA)
    found = failure_cause(error.value)
    assert (found.kind, found.detail) == cause
    assert "private-key" not in str(error.value)


def test_missing_cli_is_a_launch_failure(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(JudgeCallError) as error:
        build_judge("gemini").judge(None, "指示", [], SCHEMA)
    assert failure_cause(error.value).kind == "launch"


def test_codex_lookup_enables_only_web_search(tmp_path):
    seen = {}

    def runner(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        (tmp_path / "result.json").write_text(json.dumps(RESULT), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, b"")

    judge = CodexCliJudge(runner)
    assert judge.lookup(tmp_path, "探して", SCHEMA) == RESULT
    args = seen["args"]
    assert args[:4] == ["codex", "--search", "--ask-for-approval", "never"]
    assert args.index("--search") < args.index("exec")
    assert "shell_tool" in args
    assert "unified_exec" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--image" not in args
    assert seen["kwargs"]["timeout"] == LOOKUP_TIMEOUT


def test_claude_lookup_allows_only_web_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/bin/claude")
    seen = {}

    def run(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        response = {"type": "result", "subtype": "success", "structured_output": RESULT}
        return subprocess.CompletedProcess(args, 0, json.dumps(response).encode() + b"\n")

    monkeypatch.setattr("subprocess.run", run)
    assert ClaudeCodeJudge("").lookup(tmp_path, "探して", SCHEMA) == RESULT
    args = seen["args"]
    assert args[args.index("--tools") + 1] == "WebSearch,WebFetch"
    assert args[args.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert "Bash" not in " ".join(args)
    assert "--safe-mode" in args
    assert "--no-session-persistence" in args
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    content = json.loads(seen["kwargs"]["input"])["message"]["content"]
    assert [item["type"] for item in content] == ["text"]
    assert seen["kwargs"]["timeout"] == LOOKUP_TIMEOUT


def test_antigravity_does_not_support_lookup():
    assert not supports_lookup(build_judge("gemini"))
    assert supports_lookup(build_judge("codex"))
    assert supports_lookup(build_judge("claude"))


def test_stale_seconds_covers_lookup_timeout():
    assert LOOKUP_TIMEOUT * 2 == jobs.STALE_SECONDS
