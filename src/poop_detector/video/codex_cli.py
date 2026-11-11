"""動画の自動判定器の実装の1つ。Codex CLI を読み取り専用で実行する。"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


def codex_environment() -> dict[str, str]:
    """Codex CLI に渡す最小限の環境変数。RTSP の URL などは渡さない。"""
    keys = ("HOME", "PATH", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL")
    return {key: os.environ[key] for key in keys if key in os.environ}


class CodexCliJudge:
    """Codex CLI（codex exec）に画像と指示を渡し、JSON スキーマに沿った判定を受け取る。

    シェルやツールを無効にし、利用者の設定と規則も読まない。runner はテストで差し替える。
    """

    name = "Codex CLI"

    def __init__(self, runner: Callable[..., Any] = subprocess.run, *, model: str = "") -> None:
        self.runner = runner
        self.model = model

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
        # 同じ入力を別のモデルでも使うため、前の出力を読み間違えないようにする。
        (folder / "result.json").unlink(missing_ok=True)
        (folder / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
        image_options = [option for image in images for option in ("--image", image)]
        self.runner(
            [
                "codex",
                "--ask-for-approval",
                "never",
                "exec",
                *(["--model", self.model] if self.model else []),
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=120,
        )
        return json.loads((folder / "result.json").read_text(encoding="utf-8"))
