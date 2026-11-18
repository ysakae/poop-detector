"""判定器の CLI に推論なしでモデル一覧を問い合わせる。"""

import sys

import pytest


@pytest.mark.parametrize(
    ("provider", "executable", "program", "expected"),
    [
        (
            "gemini",
            "agy",
            """
assert sys.argv[1:] == ["models"]
print("gemini-pro-high  Gemini Pro (High)")
print("gemini-flash-medium  Gemini Flash (Medium)")
print("gemini-pro-high  Gemini Pro (High)")
print("説明だけ")
""",
            ("gemini-pro-high", "gemini-flash-medium"),
        ),
        (
            "claude",
            "claude",
            """
assert "--safe-mode" in sys.argv and "--no-session-persistence" in sys.argv
request, = sys.stdin
request = json.loads(request)
assert request["type"] == "control_request" and request["request"]["subtype"] == "initialize"
models = [{"value": value} for value in ("default", "sonnet", "opus[1m]", "haiku")]
print(json.dumps({"type": "control_response", "response": {
    "subtype": "success", "request_id": request["request_id"], "response": {"models": models},
}}))
""",
            ("sonnet", "opus[1m]", "haiku"),
        ),
        (
            "codex",
            "codex",
            """
assert sys.argv[1:] == ["app-server"]
assert json.loads(input())["method"] == "initialize"
assert json.loads(input())["method"] == "initialized"
request = json.loads(input())
assert request["method"] == "model/list"
models = [
    {"model": "has-images", "inputModalities": ["text", "image"]},
    {"model": "text-only", "inputModalities": ["text"]},
    {"model": "hidden", "hidden": True},
    {"model": "older-entry"},
]
print(json.dumps({"id": request["id"], "result": {"data": models}}), flush=True)
""",
            ("has-images", "older-entry"),
        ),
    ],
)
def test_cli_supplies_model_options(tmp_path, monkeypatch, provider, executable, program, expected):
    from poop_detector.video.judge_models import list_models

    cli = tmp_path / executable
    cli.write_text(
        f"#!{sys.executable}\nimport os, json, sys\n"
        "assert 'POOP_RTSP_URL' not in os.environ\n" + program
    )
    cli.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://dummy")
    assert list_models(provider) == expected
