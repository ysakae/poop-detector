"""環境変数を秘密情報として安全に.envへ移すスクリプトを検証する。"""

import os
import stat
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export-dotenv.py"


def run_export(root, *args, **values):
    env = {name: value for name, value in os.environ.items() if not name.startswith("POOP_")}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=root,
        env=env | values,
        capture_output=True,
        text=True,
        check=False,
    )


def test_export_writes_only_nonempty_connection_values_without_printing_them(tmp_path):
    url = "rtsp://user:${literal}'\\@camera.invalid/live#hash"
    result = run_export(tmp_path, POOP_RTSP_URL=url, POOP_NTFY_TOPIC="", POOP_OTHER="ignored")
    assert result.returncode == 0, result.stderr
    assert url not in result.stdout + result.stderr
    path = tmp_path / ".env"
    assert path.read_bytes().startswith(b"POOP_RTSP_URL=")
    assert dotenv_values(path, interpolate=False, encoding="utf-8-sig") == {"POOP_RTSP_URL": url}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_export_keeps_existing_values_unless_replace_is_requested(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# 残す設定\nPOOP_NTFY_TOPIC=old\nPOOP_NTFY_EARLY_TOPIC=\nOTHER=keep\n")
    values = {"POOP_NTFY_TOPIC": "new", "POOP_NTFY_EARLY_TOPIC": "watch"}
    result = run_export(tmp_path, **values)
    assert result.returncode == 0, result.stderr
    assert "--replace" in result.stdout
    assert dotenv_values(path, encoding="utf-8-sig") == {
        "POOP_NTFY_TOPIC": "old",
        "POOP_NTFY_EARLY_TOPIC": "watch",
        "OTHER": "keep",
    }
    assert run_export(tmp_path, "--replace", **values).returncode == 0
    assert dotenv_values(path, encoding="utf-8-sig")["POOP_NTFY_TOPIC"] == "new"
    assert "# 残す設定" in path.read_text(encoding="utf-8-sig")
