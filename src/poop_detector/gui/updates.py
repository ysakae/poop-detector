"""PyPI の安定版を確認し、切り替え先の仮想環境を準備する。"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from http.client import HTTPException
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import monotonic
from urllib.request import urlopen

from packaging.version import InvalidVersion, Version

PYPI_JSON = "https://pypi.org/pypi/poop-detector/json"
PYPI_INDEX = "https://pypi.org/simple"
CHECK_INTERVAL = 300
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
INSTALL_TIMEOUT = 180


@lru_cache(maxsize=1)
def installed_version() -> str:
    return version("poop-detector")


def stable_version(releases: object) -> str | None:
    if not isinstance(releases, dict):
        return None
    stable = []
    for name, files in releases.items():
        try:
            release = Version(name)
        except (TypeError, InvalidVersion):
            continue
        if release.is_prerelease or release.local is not None or not isinstance(files, list):
            continue
        if any(
            isinstance(file, dict)
            and file.get("packagetype") == "bdist_wheel"
            and file.get("yanked") is False
            for file in files
        ):
            stable.append(release)
    return str(max(stable)) if stable else None


@lru_cache(maxsize=1)
def _check_latest(_period: int) -> str | None:
    try:
        # ブラウザーから渡された URL やサーバーの認証情報は使わない。
        with urlopen(PYPI_JSON, timeout=4) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) <= MAX_RESPONSE_BYTES:
            document = json.loads(payload)
            if isinstance(document, dict):
                return stable_version(document.get("releases"))
    except (OSError, ValueError, HTTPException):
        pass
    return None


def latest_stable() -> str | None:
    return _check_latest(int(monotonic() // CHECK_INTERVAL))


def is_newer(candidate: str | None, current: str) -> bool:
    return candidate is not None and Version(candidate) > Version(current)


class Upgrade:
    """現行の環境とデータを保持したまま、更新先を用意する。"""

    def __init__(self, directory: Path, uv: str) -> None:
        self.directory = directory
        self.uv = uv
        self.phase = "idle"
        self.target: str | None = None
        self.python: Path | None = None

    @property
    def marker(self) -> Path:
        return self.directory / "active"

    def active_python(self) -> Path | None:
        try:
            name = self.marker.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if not name:
            return None
        if not name.startswith("release-") or Path(name).name != name:
            raise ValueError("更新先の記録が無効です")
        environment = self.directory / name
        if environment.resolve() == Path(sys.prefix).resolve():
            return None
        if not (environment / "pyvenv.cfg").is_file():
            raise ValueError("更新先の仮想環境が見つかりません")
        return environment / "bin" / "python"

    def prepare(self, target: str) -> Path:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        environment = Path(tempfile.mkdtemp(prefix="release-", dir=self.directory))
        python = environment / "bin" / "python"
        extras = [
            extra
            for extra, dependency in (
                ("analysis", "scikit-learn"),
                ("audio-model", "ai-edge-litert"),
            )
            if is_installed(dependency)
        ]
        package = f"poop-detector[{','.join(extras)}]" if extras else "poop-detector"
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("UV_", "PIP_", "POOP_", "PYTHON"))
        }
        try:
            commands = (
                [self.uv, "--no-config", "venv", "--python", sys.executable, str(environment)],
                [
                    self.uv,
                    "--no-config",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--default-index",
                    PYPI_INDEX,
                    "--only-binary",
                    "poop-detector",
                    f"{package}=={target}",
                ],
                [str(python), "-I", "-m", "poop_detector.main", "--version"],
            )
            # 実行先は検出済みの uv と、この処理で作った Python に限る。
            for command in commands:
                result = subprocess.run(  # noqa: S603
                    command,
                    env=env,
                    cwd=environment,
                    capture_output=True,
                    check=True,
                    timeout=INSTALL_TIMEOUT,
                )
            if result.stdout.decode().strip() != target:
                raise ValueError("更新先の CLI バージョンが一致しません")
        except (OSError, ValueError, subprocess.SubprocessError):
            shutil.rmtree(environment, ignore_errors=True)
            raise
        return python

    def activate(self, python: Path, argv: list[str]) -> None:
        previous = self.marker.read_bytes() if self.marker.exists() else None
        name = python.parent.parent.name
        self._write_marker(self.directory / "previous", previous or b"")
        self._write_marker(self.marker, (name + "\n").encode())
        try:
            exec_python(python, argv)
        except OSError:
            if previous is None:
                self.marker.unlink()
            else:
                self._write_marker(self.marker, previous)
            raise

    def _write_marker(self, destination: Path, content: bytes) -> None:
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as temporary:
            temporary.write(content)
        pending = Path(temporary.name)
        try:
            pending.replace(destination)
        finally:
            pending.unlink(missing_ok=True)


def is_installed(name: str) -> bool:
    try:
        version(name)
    except PackageNotFoundError:
        return False
    return True


def exec_python(python: Path, argv: list[str]) -> None:
    # uv run の VIRTUAL_ENV や PYTHONPATH が旧版を参照していても更新先を読み込む。
    env = {**os.environ, "VIRTUAL_ENV": str(python.parent.parent)}
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", os.defpath)
    # 実行先は指定ディレクトリで準備して起動を確認した Python に限る。
    os.execve(python, [str(python), "-I", "-m", "poop_detector.main", *argv], env)  # noqa: S606
