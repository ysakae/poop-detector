"""video-benchmark が保存した比較の結果を、画面に出せる要約として読む。

比較の実行は時間がかかるので CLI に残し、GUI は既定の保存先（data/benchmarks）の JSON だけを
読む。結果は人が作ったファイルとして扱い、形が違うものは数えるだけで表示しない。
動画の ID などの個々の行は返さず、判定器ごとの集計だけを返す。
"""

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

RESULTS_DIR = "benchmarks"
REPORT_VERSION = 1
# 読むファイルの数と大きさの上限。古いものや大きすぎるものは読まない。
SCAN_LIMIT = 20
FILE_LIMIT_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class JudgeScore:
    """1つの判定器:モデルの集計。適合率・再現率は 0〜1 で、分母が0なら None。"""

    name: str
    cases: int
    feces_precision: float | None
    feces_recall: float | None
    urine_precision: float | None
    urine_recall: float | None
    uncertain: int
    errors: int


@dataclass(frozen=True)
class BenchmarkResult:
    """1回の比較の結果。"""

    created_at: str
    dataset: str
    scores: tuple[JudgeScore, ...]


def results_dir(root: str | Path) -> Path:
    return Path(root) / RESULTS_DIR


def default_output(root: str | Path, now: datetime) -> Path:
    """--output を省いたときの保存先。作成時刻（UTC）の名前の新しいファイル。"""
    return results_dir(root) / f"{now.strftime('%Y%m%d-%H%M%S')}.json"


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _ratio(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError
    return float(value)


def _score(name: object, summary: object) -> JudgeScore:
    if not isinstance(name, str) or not isinstance(summary, dict):
        raise ValueError
    types = summary["types"]
    return JudgeScore(
        name=name,
        cases=_count(summary["cases"]),
        feces_precision=_ratio(types["feces"]["precision"]),
        feces_recall=_ratio(types["feces"]["recall"]),
        urine_precision=_ratio(types["urine"]["precision"]),
        urine_recall=_ratio(types["urine"]["recall"]),
        uncertain=_count(summary["uncertain"]),
        errors=_count(summary["errors"]),
    )


def _parse(report: Any) -> BenchmarkResult:
    if not isinstance(report, dict) or report.get("version") != REPORT_VERSION:
        raise ValueError
    created_at, dataset = report["created_at"], report.get("dataset", "")
    summaries = report["summaries"]
    if not isinstance(created_at, str) or not isinstance(dataset, str):
        raise ValueError
    # 並べ替えで比べられるよう、タイムゾーン付きの時刻だけを受け付ける。
    if datetime.fromisoformat(created_at).tzinfo is None:
        raise ValueError
    if not isinstance(summaries, dict) or not summaries:
        raise ValueError
    scores = tuple(_score(name, summary) for name, summary in summaries.items())
    return BenchmarkResult(created_at, dataset, scores)


def _read(path: Path) -> BenchmarkResult:
    if path.stat().st_size > FILE_LIMIT_BYTES:
        raise ValueError
    return _parse(json.loads(path.read_text(encoding="utf-8")))


def load_results(root: str | Path, limit: int = 5) -> tuple[list[BenchmarkResult], int]:
    """新しい順の比較の結果と、読めなかったファイルの数。"""
    folder = results_dir(root)
    if not folder.is_dir():
        return [], 0
    files = sorted(
        (path for path in folder.glob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:SCAN_LIMIT]
    results = []
    unreadable = 0
    for path in files:
        try:
            results.append(_read(path))
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
            unreadable += 1
    results.sort(key=lambda result: datetime.fromisoformat(result.created_at), reverse=True)
    return results[:limit], unreadable
