"""外部 CLI に切り出し画像を渡し、運用環境からの秘密と失敗を適切に扱う。"""

import json
import subprocess
import sys

import pytest

from poop_detector.commands.context import CommandError
from poop_detector.commands.review import ready_judge
from poop_detector.config import Config
from poop_detector.video.providers import build_judge

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
        response = {"type": "result", "subtype": "success", "structured_output": RESULT}
        stdout = b'{"type":"system","subtype":"init"}\n' + json.dumps(response).encode() + b"\n"
        return subprocess.CompletedProcess(args, 0, stdout)

    monkeypatch.setattr("subprocess.run", run)
    judge = ready_judge(Config(review_provider="claude", review_model="sonnet"))
    assert judge.name == "Claude Code CLI"
    assert judge.judge(tmp_path, "指示", ["frames.jpg", "differences.jpg"], SCHEMA) == RESULT


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
    ("scenario", "expected_error"),
    [
        ("ok", None),
        ("auth", "agy acp --login"),
        ("model", "モデル"),
        ("tool", "ツール"),
        ("invalid", "有効な判定"),
    ],
)
def test_antigravity_cli_exchanges_acp_messages(tmp_path, monkeypatch, scenario, expected_error):
    executable = tmp_path / "agy"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"scenario = {scenario!r}\n"
        "assert sys.argv[1:] == ['acp', '--sandbox', '--include-default-customizations=false']\n"
        "assert 'GEMINI_API_KEY' not in os.environ and 'POOP_RTSP_URL' not in os.environ\n"
        "assert not (Path.cwd() / 'frames.jpg').exists()\n"
        "def send(**message):\n"
        "    print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    method, params = request['method'], request['params']\n"
        "    if method == 'initialize':\n"
        "        assert params == {'protocolVersion': 1, 'clientCapabilities': {}}\n"
        "        result = {'protocolVersion': 1, 'agentCapabilities': "
        "{'promptCapabilities': {'image': True}}}\n"
        "    elif method == 'session/new':\n"
        "        assert params == {'cwd': str(Path.cwd()), 'mcpServers': []}\n"
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
        "        if scenario == 'tool':\n"
        "            send(method='session/update', params={'sessionId': 'test', "
        "'update': {'sessionUpdate': 'tool_call'}})\n"
        "        else:\n"
        "            for chunk in ('{\"label\":', '\"none\"}') if scenario == 'ok' else ('invalid',):\n"
        "                send(method='session/update', params={'sessionId': 'test', 'update': {"
        "'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': chunk}}})\n"
        "        result = {'stopReason': 'end_turn'}\n"
        "    elif method == 'session/close':\n"
        "        result = {}\n"
        "    else:\n"
        "        raise AssertionError(method)\n"
        "    send(id=request['id'], result=result)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setattr("shutil.which", lambda _: str(executable))
    monkeypatch.setenv("GEMINI_API_KEY", "private-key")
    monkeypatch.setenv("POOP_RTSP_URL", "private-url")
    (tmp_path / "frames.jpg").write_bytes(b"frames")
    if scenario in ("auth", "model"):
        with pytest.raises(CommandError, match=expected_error):
            ready_judge(Config(review_provider="gemini", review_model="gemini-test"))
        return
    judge = ready_judge(Config(review_provider="gemini", review_model="gemini-test"))
    assert judge.name == "Antigravity CLI"
    if expected_error:
        with pytest.raises(ValueError, match=expected_error):
            judge.judge(tmp_path, "指示", ["frames.jpg"], SCHEMA)
    else:
        assert judge.judge(tmp_path, "指示", ["frames.jpg"], SCHEMA) == RESULT
