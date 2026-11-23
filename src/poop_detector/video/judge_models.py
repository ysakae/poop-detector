"""動画判定を実行せず、各判定器のモデル候補を取得する。"""

import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from functools import partial

from ..config import valid_review_model
from .codex_cli import app_server_request
from .gemini_api import list_gemini_models
from .providers import RESPONSE_LIMIT, _cli_environment, _executable

TIMEOUT = 8
UNAVAILABLE = "モデル一覧を取得できません。判定器の認証と接続を確認してください。"


def _run(command: list[str], data: bytes = b"") -> bytes:
    try:
        # PATH から解決した CLI を、一覧の取得に必要な固定引数だけで実行する。
        result = subprocess.run(  # noqa: S603
            command,
            input=data,
            env=_cli_environment(),
            cwd=tempfile.gettempdir(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError(UNAVAILABLE) from None
    if result.returncode or len(result.stdout) > RESPONSE_LIMIT:
        raise ValueError(UNAVAILABLE)
    return result.stdout


def _codex() -> Iterable[object]:
    executable = _executable("codex", "Codex CLI")
    response = app_server_request(
        executable, "model/list", timeout=TIMEOUT, params={"includeHidden": False}
    )
    return (
        item.get("model")
        for item in response["result"]["data"]
        if not item.get("hidden") and "image" in (item.get("inputModalities") or ["text", "image"])
    )


def _claude() -> Iterable[object]:
    executable = _executable("claude", "Claude Code CLI")
    command = [
        executable,
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--safe-mode",
        "--tools",
        "",
        "--no-session-persistence",
    ]
    # 初期化の応答に候補が含まれる。ユーザーメッセージは送らない。
    request = {
        "type": "control_request",
        "request_id": "models",
        "request": {"subtype": "initialize"},
    }
    for line in _run(command, json.dumps(request).encode() + b"\n").splitlines():
        message = json.loads(line)
        if message.get("type") != "control_response":
            continue
        response = message["response"]
        if response.get("request_id") == "models" and response.get("subtype") == "success":
            return (model.get("value") for model in response["response"]["models"])
    raise ValueError(UNAVAILABLE)


def _antigravity() -> Iterable[object]:
    executable = _executable("agy", "Antigravity CLI")
    lines = _run([executable, "models"]).decode().splitlines()
    # 行ごとに「モデルID  表示名」の形で出る。保存に使う ID だけを読む。
    return (match[1] for line in lines if (match := re.match(r"^\s*(\S+)[ \t]{2,}\S", line)))


def list_models(provider: str) -> tuple[str, ...]:
    """設定に保存できるモデル名を重複なしに返す。"""
    readers: dict[str, Callable[[], Iterable[object]]] = {
        "codex": _codex,
        "claude": _claude,
        "gemini": _antigravity,
        "gemini-api": partial(list_gemini_models, TIMEOUT),
    }
    try:
        models = tuple(
            dict.fromkeys(
                model
                for model in readers[provider]()
                if isinstance(model, str)
                and model
                and valid_review_model(model)
                and (provider != "claude" or model != "default")
            )
        )
    except (ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        raise ValueError(UNAVAILABLE) from None
    if not models:
        raise ValueError(UNAVAILABLE)
    return models
