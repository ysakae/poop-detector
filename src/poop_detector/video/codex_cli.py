"""動画の自動判定器の実装の1つ。Codex CLI を読み取り専用で実行する。"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .token_usage import TokenUsage, parse_codex

JUDGE_TIMEOUT = 120
# ウェブ検索は動画の判定より時間がかかる。advice.jobs.STALE_SECONDS はこの2倍。
LOOKUP_TIMEOUT = 300


def codex_environment() -> dict[str, str]:
    """Codex CLI に渡す最小限の環境変数。RTSP の URL などは渡さない。"""
    keys = ("HOME", "PATH", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL")
    return {key: os.environ[key] for key in keys if key in os.environ}


def app_server_request(
    executable: str,
    method: str,
    *,
    timeout: int,
    params: dict[str, Any] | None = None,
    spawn: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """app-server に1件だけ読み取りを要求し、応答後または時間切れに終了する。"""
    requests = (
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "poop-detector", "version": "0"}},
        },
        {"method": "initialized"},
        {"id": 2, "method": method, **({"params": params} if params is not None else {})},
    )
    try:
        # PATH から解決した CLI を、固定の引数で実行する。
        process = spawn(
            [executable, "app-server"],
            env=codex_environment(),
            cwd=tempfile.gettempdir(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise ValueError("Codex CLI を起動できません") from None
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    try:
        process.stdin.write(b"".join(json.dumps(request).encode() + b"\n" for request in requests))
        process.stdin.flush()
        while line := process.stdout.readline(1024 * 1024):
            message = json.loads(line)
            if isinstance(message, dict) and message.get("id") == 2:
                return message
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=timeout)
    raise ValueError("Codex CLI から応答を取得できません")


class CodexCliJudge:
    """Codex CLI（codex exec）に画像と指示を渡し、JSON スキーマに沿った判定を受け取る。

    シェルやツールを無効にし、利用者の設定と規則も読まない。runner はテストで差し替える。
    """

    name = "Codex CLI"

    def __init__(
        self, runner: Callable[..., Any] = subprocess.run, *, model: str = "", effort: str = ""
    ) -> None:
        self.runner = runner
        self.model = model
        # 推論の深さ（low など）。空なら CLI の既定。
        self.effort = effort
        # 直近の呼び出しで使ったトークン数。CLI が出さなければ None。
        self.last_usage: TokenUsage | None = None

    def check(self) -> None:
        """Codex CLI がインストール済みでログインしているか確かめる。"""
        if not shutil.which("codex"):
            raise ValueError("Codex CLIをインストールし、codex loginでログインしてください")
        try:
            # 固定の引数で、PATH 上の Codex CLI のログイン状態を確かめるだけ。
            result = subprocess.run(
                ["codex", "login", "status"],  # noqa: S607
                capture_output=True,
                check=False,
                timeout=10,
                env=codex_environment(),
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Codex CLIのログイン状態を確認できません") from None
        if result.returncode != 0:
            raise ValueError("codex loginでCodex CLIにログインしてください")

    def judge(
        self, folder: Path, prompt: str, images: Sequence[str], schema: dict[str, Any]
    ) -> object:
        return self._invoke(folder, prompt, images, schema, search=False, timeout=JUDGE_TIMEOUT)

    def lookup(self, folder: Path, prompt: str, schema: dict[str, Any]) -> object:
        """ウェブ検索だけを許して調べさせる（画像なし）。シェルとファイルの書き込みは無効のまま。"""
        return self._invoke(folder, prompt, (), schema, search=True, timeout=LOOKUP_TIMEOUT)

    def _invoke(
        self,
        folder: Path,
        prompt: str,
        images: Sequence[str],
        schema: dict[str, Any],
        *,
        search: bool,
        timeout: int,
    ) -> object:
        # 同じ入力を別のモデルでも使うため、前の出力を読み間違えないようにする。
        (folder / "result.json").unlink(missing_ok=True)
        (folder / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
        image_options = [option for image in images for option in ("--image", image)]
        self.last_usage = None
        # 標準出力には --json のイベントだけが出る。使用量の数だけを読み、応答の本文は捨てる。
        completed = self.runner(
            [
                "codex",
                *(["--search"] if search else []),
                "--ask-for-approval",
                "never",
                "exec",
                "--json",
                *(["--model", self.model] if self.model else []),
                # 利用者の設定は読まないので、推論の深さは設定の上書きとして渡す。
                *(["-c", f'model_reasoning_effort="{self.effort}"'] if self.effort else []),
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
                "--disable",
                "shell_tool",
                "--disable",
                "unified_exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--output-schema",
                "schema.json",
                "--output-last-message",
                "result.json",
                prompt,
                *image_options,
            ],
            cwd=folder,
            env=codex_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=timeout,
        )
        self.last_usage = parse_codex(getattr(completed, "stdout", None))
        return json.loads((folder / "result.json").read_text(encoding="utf-8"))
