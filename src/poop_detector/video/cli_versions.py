"""判定器の CLI の版を読み、比較の結果に残す。読めなくても比較は止めない。"""

import shutil
import subprocess
import tempfile

from .providers import _cli_environment

# 設定の review.provider の値と、版を尋ねる CLI のコマンド。
COMMANDS = {"codex": "codex", "claude": "claude", "gemini": "agy"}
VERSION_TIMEOUT = 15
# 版の表示として受け付ける長さの上限。
VERSION_LIMIT = 200


def cli_version(provider: str) -> str | None:
    """CLI の --version の1行目。CLI がない、失敗した、表示が不正なら None。"""
    command = COMMANDS.get(provider)
    executable = shutil.which(command) if command else None
    if executable is None:
        return None
    try:
        # PATH から解決した CLI を、固定の引数で実行する。環境変数は判定と同じものだけを渡す。
        result = subprocess.run(  # noqa: S603
            [executable, "--version"],
            env=_cli_environment(),
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=VERSION_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    lines = result.stdout.decode(errors="replace").strip().splitlines()
    line = lines[0].strip() if lines else ""
    if not line or len(line) > VERSION_LIMIT or not line.isprintable():
        return None
    return line
