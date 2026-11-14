"""判定器の CLI のプランの使用量（5時間・週間の消費割合）を、各 CLI に問い合わせて読む。

どの CLI もモデルを呼ばない方法で問い合わせる（2026年11月に次の版で確かめた）。

- Codex CLI 0.167：`codex app-server` の `account/rateLimits/read`（実験的な窓口）
- Claude Code CLI 2.1：`claude --print /usage --max-turns 0` の表示の文章
- Antigravity CLI 1.5：`agy --print /usage --output-format json` の `command.data`

値は型と範囲を確かめた割合と時刻だけを取り出し、アカウントの ID や応答の本文は持たない。
CLI の版が変わって形が合わなければ「不明」として扱う。結果は GUI のメモリーにだけ置き、
データベースには保存しない。
"""

import json
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .codex_cli import codex_environment
from .providers import _cli_environment

TIMEOUT = 20
RESPONSE_LIMIT = 1024 * 1024
NAME_LIMIT = 40
FIVE_HOURS, ONE_WEEK = 300, 10080
UNKNOWN = "不明（CLI のログインと版を確認してください）"
NOT_INSTALLED = "CLI がインストールされていません"


class NotInstalledError(ValueError):
    """CLI が PATH にない。"""


@dataclass(frozen=True)
class QuotaWindow:
    """1つの制限の窓（5時間・週間）の消費割合と、次に戻る時刻（分からなければ None）。"""

    used_percent: int
    resets_at: datetime | None


@dataclass(frozen=True)
class QuotaGroup:
    """同じ制限を共有するモデルのまとまりと、その5時間・週間の窓。"""

    name: str
    five_hour: QuotaWindow | None
    weekly: QuotaWindow | None


@dataclass(frozen=True)
class PlanUsage:
    """1つの判定器のプランの使用量。読めなければ groups は空で、problem に理由がある。"""

    provider: str
    groups: tuple[QuotaGroup, ...]
    problem: str | None
    checked_at: datetime


Reader = Callable[[], tuple[QuotaGroup, ...]]


def _percent(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 100:
        raise ValueError("使用量の割合が不正です")
    return round(value)


def _name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("制限の名前が不正です")
    return value.strip()[:NAME_LIMIT]


def _codex_window(value: object) -> QuotaWindow | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Codex CLI の制限の形が不正です")
    resets = value.get("resetsAt")
    if resets is not None and (isinstance(resets, bool) or not isinstance(resets, int)):
        raise ValueError("Codex CLI の制限の時刻が不正です")
    return QuotaWindow(
        _percent(value.get("usedPercent")),
        None if resets is None else datetime.fromtimestamp(resets, UTC),
    )


def parse_codex(message: object) -> tuple[QuotaGroup, ...]:
    """account/rateLimits/read の応答から、窓の長さで5時間と週間を選ぶ。"""
    try:
        return _codex_groups(message)
    except (ValueError, TypeError, OverflowError, OSError):
        raise ValueError("Codex CLI から使用量を取得できません") from None


def _codex_groups(message: object) -> tuple[QuotaGroup, ...]:
    if not isinstance(message, dict) or not isinstance(message.get("result"), dict):
        raise ValueError("Codex CLI から使用量を取得できません")
    limits = message["result"].get("rateLimits")
    if not isinstance(limits, dict):
        raise ValueError("Codex CLI から使用量を取得できません")
    windows: dict[int, QuotaWindow] = {}
    for position, key in ((FIVE_HOURS, "primary"), (ONE_WEEK, "secondary")):
        raw = limits.get(key)
        window = _codex_window(raw)
        if window is None or not isinstance(raw, dict):
            continue
        # 窓の長さがなければ、primary を5時間、secondary を週間とみなす。
        length = raw.get("windowDurationMins") or position
        windows[length] = window
    five_hour, weekly = windows.get(FIVE_HOURS), windows.get(ONE_WEEK)
    if five_hour is None and weekly is None:
        raise ValueError("Codex CLI から使用量を取得できません")
    return (QuotaGroup("Codex", five_hour, weekly),)


CLAUDE_SESSION = re.compile(r"^Current session: (\d{1,3})% used", re.MULTILINE)
CLAUDE_WEEK = re.compile(r"^Current week \(([^()\n]{1,40})\): (\d{1,3})% used", re.MULTILINE)


def parse_claude(output: bytes) -> tuple[QuotaGroup, ...]:
    """/usage の表示から、セッション（5時間）と週間の割合だけを読む。リセットの時刻は読まない。"""
    try:
        payload = json.loads(output)
        text = payload["result"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("Claude Code CLI から使用量を取得できません") from None
    if not isinstance(text, str):
        raise ValueError("Claude Code CLI から使用量を取得できません")
    session = CLAUDE_SESSION.search(text)
    weeks = {name: int(percent) for name, percent in CLAUDE_WEEK.findall(text)}
    if session is None and not weeks:
        raise ValueError("Claude Code CLI から使用量を取得できません")
    all_models = weeks.pop("all models", None)
    groups = [
        QuotaGroup(
            "全モデル",
            None if session is None else QuotaWindow(_percent(int(session[1])), None),
            None if all_models is None else QuotaWindow(_percent(all_models), None),
        )
    ]
    groups.extend(
        QuotaGroup(name[:NAME_LIMIT], None, QuotaWindow(_percent(percent), None))
        for name, percent in weeks.items()
    )
    return tuple(groups)


def _agy_window(bucket: object) -> tuple[str, QuotaWindow]:
    if not isinstance(bucket, dict) or bucket.get("window") not in ("5h", "weekly"):
        raise ValueError("Antigravity CLI の制限の形が不正です")
    remaining = bucket.get("remaining_fraction")
    if isinstance(remaining, bool) or not isinstance(remaining, int | float):
        raise ValueError("Antigravity CLI の制限の割合が不正です")
    if not 0 <= remaining <= 1:
        raise ValueError("Antigravity CLI の制限の割合が不正です")
    reset = bucket.get("reset_time")
    resets_at = None
    if isinstance(reset, str):
        resets_at = datetime.fromisoformat(reset)
        if resets_at.tzinfo is None:
            raise ValueError("Antigravity CLI の制限の時刻が不正です")
    return bucket["window"], QuotaWindow(_percent((1 - remaining) * 100), resets_at)


def parse_antigravity(output: bytes) -> tuple[QuotaGroup, ...]:
    """/usage の JSON から、まとまりごとに残りの割合を消費割合に直す。"""
    try:
        payload: Any = json.loads(output)
        command = payload["command"]
        if command["name"] != "usage":
            raise ValueError
        groups = command["data"]["groups"]
        if not isinstance(groups, list) or not groups:
            raise ValueError
        found = []
        for group in groups:
            windows = dict(_agy_window(bucket) for bucket in group["buckets"])
            found.append(QuotaGroup(_name(group["name"]), windows.get("5h"), windows.get("weekly")))
    except (ValueError, KeyError, TypeError):
        raise ValueError("Antigravity CLI から使用量を取得できません") from None
    return tuple(found)


def _resolve(command: str, which: Callable[[str], str | None]) -> str:
    executable = which(command)
    if executable is None:
        raise NotInstalledError(NOT_INSTALLED)
    return executable


def _run(runner: Callable[..., Any], command: list[str], env: dict[str, str]) -> bytes:
    try:
        # PATH から解決した CLI を、固定の引数で実行する。
        result = runner(
            command,
            env=env,
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("CLI から使用量を取得できません") from None
    if result.returncode or len(result.stdout) > RESPONSE_LIMIT:
        raise ValueError("CLI から使用量を取得できません")
    output: bytes = result.stdout
    return output


def read_claude(
    runner: Callable[..., Any] = subprocess.run, which: Callable[[str], str | None] = shutil.which
) -> tuple[QuotaGroup, ...]:
    executable = _resolve("claude", which)
    command = [
        executable,
        "--print",
        "/usage",
        "--output-format",
        "json",
        "--safe-mode",
        "--tools",
        "",
        "--no-session-persistence",
        "--max-turns",
        "0",
    ]
    return parse_claude(_run(runner, command, _cli_environment()))


def read_antigravity(
    runner: Callable[..., Any] = subprocess.run, which: Callable[[str], str | None] = shutil.which
) -> tuple[QuotaGroup, ...]:
    executable = _resolve("agy", which)
    command = [executable, "--print", "/usage", "--output-format", "json", "--sandbox"]
    return parse_antigravity(_run(runner, command, _cli_environment()))


CODEX_REQUESTS = (
    {
        "id": 1,
        "method": "initialize",
        "params": {"clientInfo": {"name": "poop-detector", "version": "0"}},
    },
    {"method": "initialized"},
    {"id": 2, "method": "account/rateLimits/read"},
)


def read_codex(
    spawn: Callable[..., Any] = subprocess.Popen, which: Callable[[str], str | None] = shutil.which
) -> tuple[QuotaGroup, ...]:
    """app-server に制限の読み取りだけを要求する。標準入力を閉じると応答前に終わるので開けておく。"""
    executable = _resolve("codex", which)
    try:
        # PATH から解決した Codex CLI を、固定の引数で実行する。
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
    # 応答がなければ時間切れで止め、読み取りを終わらせる。
    timer = threading.Timer(TIMEOUT, process.kill)
    timer.start()
    try:
        process.stdin.write(b"".join(json.dumps(r).encode() + b"\n" for r in CODEX_REQUESTS))
        process.stdin.flush()
        while line := process.stdout.readline(RESPONSE_LIMIT):
            message = json.loads(line)
            if isinstance(message, dict) and message.get("id") == 2:
                return parse_codex(message)
        raise ValueError("Codex CLI から使用量を取得できません")
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Codex CLI から使用量を取得できません") from None
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=TIMEOUT)


def read_plan_usage(provider: str, reader: Reader, now: datetime | None = None) -> PlanUsage:
    """1つの CLI を問い合わせる。失敗の詳細（CLI の出力）は出さず、案内の文言にする。"""
    checked_at = now or datetime.now(UTC)
    try:
        return PlanUsage(provider, reader(), None, checked_at)
    except NotInstalledError:
        return PlanUsage(provider, (), NOT_INSTALLED, checked_at)
    except ValueError:
        return PlanUsage(provider, (), UNKNOWN, checked_at)


# 設定の review.provider の値ごとの読み取り方（providers.PROVIDER_NAMES と同じ順）。
READERS: Mapping[str, Reader] = {
    "codex": read_codex,
    "claude": read_claude,
    "gemini": read_antigravity,
}


class PlanUsageBoard:
    """直近に問い合わせたプランの使用量。GUI の画面ごとに CLI を起動しないよう、手動で更新する。"""

    def __init__(self, readers: Mapping[str, Reader] = READERS) -> None:
        self.readers = readers
        self.lock = threading.Lock()
        self.latest: dict[str, PlanUsage] = {}

    def refresh(self) -> None:
        """すべての CLI に並行して問い合わせ、結果を入れ替える。"""
        providers = list(self.readers)
        with ThreadPoolExecutor(max_workers=len(providers) or 1) as pool:
            found = list(
                pool.map(lambda name: read_plan_usage(name, self.readers[name]), providers)
            )
        with self.lock:
            self.latest = {usage.provider: usage for usage in found}

    def snapshot(self) -> dict[str, PlanUsage]:
        with self.lock:
            return dict(self.latest)
