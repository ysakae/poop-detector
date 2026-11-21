"""判定器を呼んだ記録（judge_runs テーブル）と、期間別の回数の集計。

判定の台帳（video_assessments）は動画1本につき1行で、失敗した判定は再試行で上書きされる。
使用量は CLI を呼んだ回数で数えたいので、呼ぶたびに1行を追記する別の表に分けている。
記録するのは判定器・モデル・用途・時刻・成否と失敗の種類（judge_failures）だけで、
CLI の応答や画像は入れない。
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..records.database import Database
from .judge_failures import FailureCause, JudgeCallError, failure_cause
from .token_usage import TokenUsage

if TYPE_CHECKING:
    from .review import VideoJudge

logger = logging.getLogger(__name__)

# 監視と video-review の判定（review）と、判定器の比較（benchmark）。
PURPOSES = ("review", "benchmark")


@dataclass(frozen=True)
class JudgeRunUsage:
    """判定器・モデル・用途ごとの、期間別の呼び出し回数と直近の呼び出し。"""

    provider: str
    model: str | None
    purpose: str
    today: int
    last_7_days: int
    last_30_days: int
    this_month: int
    total: int
    failures: int
    last_at: str
    last_failed: bool
    # 直近30日のうち、CLI がトークン数を出した呼び出しの回数と、その合計。
    measured: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0


def _boundaries(now: datetime) -> tuple[str, str, str, str]:
    """今日・7日前・30日前・今月の始まり（ローカル時刻で区切り、UTC の ISO 8601 で返す）。"""
    local = now.astimezone()
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)

    def utc(moment: datetime) -> str:
        return moment.astimezone(UTC).isoformat()

    return utc(today), utc(local - timedelta(days=7)), utc(local - timedelta(days=30)), utc(month)


class JudgeRunLog:
    """judge_runs テーブル。判定器を呼ぶたびに1行を追記し、期間別に数える。"""

    def __init__(self, database: Database) -> None:
        self.connect = database.connect

    def record(
        self,
        provider: str,
        model: str | None,
        purpose: str,
        *,
        failed: bool,
        at: datetime | None = None,
        usage: TokenUsage | None = None,
        failure: str | None = None,
        failure_detail: str | None = None,
    ) -> None:
        if purpose not in PURPOSES:
            raise ValueError(f"判定器の用途は {'、'.join(PURPOSES)} のいずれかです")
        # 種類と詳細の形を確かめる（決まった種類と、番号などの短い語だけを残す）。
        cause = JudgeCallError("", failure, failure_detail) if failure else None
        stamp = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO judge_runs (provider, model, purpose, ran_at, failed,"
                " input_tokens, cached_tokens, output_tokens, failure, failure_detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    provider,
                    model,
                    purpose,
                    stamp,
                    int(failed),
                    usage.input_tokens if usage else None,
                    usage.cached_tokens if usage else None,
                    usage.output_tokens if usage else None,
                    cause.kind if cause else None,
                    cause.detail if cause else None,
                ),
            )

    def failure_counts(
        self, since: datetime, purpose: str = "review"
    ) -> list[tuple[str | None, int]]:
        """since 以降に purpose で失敗した呼び出しを、種類ごとに数える（多い順）。

        種類を記録する前の失敗は、種類を None として数える。
        """
        with self.connect() as db:
            rows = db.execute(
                """SELECT failure, COUNT(*) AS n FROM judge_runs
                WHERE failed = 1 AND purpose = ? AND julianday(ran_at) >= julianday(?)
                GROUP BY failure ORDER BY n DESC, failure IS NULL, failure""",
                (purpose, since.astimezone(UTC).isoformat()),
            ).fetchall()
        return [(row["failure"], int(row["n"])) for row in rows]

    def count_since(self, since: datetime) -> int:
        """since 以降に判定器を呼んだ回数（用途と成否を問わない）。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM judge_runs WHERE julianday(ran_at) >= julianday(?)",
                (since.astimezone(UTC).isoformat(),),
            ).fetchone()
        return int(row[0])

    def trailing_failures(self, purpose: str = "review") -> int:
        """purpose の呼び出しが、直近で続けて失敗している回数（最後が成功なら 0）。

        最後の成功を1回探し、それより新しい呼び出しを数える。同じ時刻なら、あとに書いた行
        （id が大きい）を新しいとみなす。行の組み合わせを数えると、記録が増えるほど遅くなる。
        """
        with self.connect() as db:
            last = db.execute(
                """SELECT julianday(ran_at), id FROM judge_runs WHERE purpose = ? AND failed = 0
                ORDER BY julianday(ran_at) DESC, id DESC LIMIT 1""",
                (purpose,),
            ).fetchone()
            if last is None:
                row = db.execute(
                    "SELECT COUNT(*) FROM judge_runs WHERE purpose = ?", (purpose,)
                ).fetchone()
            else:
                row = db.execute(
                    """SELECT COUNT(*) FROM judge_runs WHERE purpose = ?
                    AND (julianday(ran_at), id) > (?, ?)""",
                    (purpose, last[0], last[1]),
                ).fetchone()
        return int(row[0])

    def usage(self, now: datetime | None = None) -> list[JudgeRunUsage]:
        """判定器・モデル・用途ごとの回数。直近に使ったものから並べる。"""
        today, week, month_window, month = _boundaries(now or datetime.now(UTC))
        with self.connect() as db:
            rows = db.execute(
                """SELECT provider, model, purpose,
                SUM(julianday(ran_at) >= julianday(?)) AS today,
                SUM(julianday(ran_at) >= julianday(?)) AS last_7_days,
                SUM(julianday(ran_at) >= julianday(?)) AS last_30_days,
                SUM(julianday(ran_at) >= julianday(?)) AS this_month,
                COUNT(*) AS total, SUM(failed) AS failures, MAX(ran_at) AS last_at,
                SUM(input_tokens IS NOT NULL AND julianday(ran_at) >= julianday(?)) AS measured,
                TOTAL(CASE WHEN julianday(ran_at) >= julianday(?) THEN input_tokens END) AS input,
                TOTAL(CASE WHEN julianday(ran_at) >= julianday(?) THEN cached_tokens END) AS cached,
                TOTAL(CASE WHEN julianday(ran_at) >= julianday(?) THEN output_tokens END) AS output,
                (SELECT b.failed FROM judge_runs b
                 WHERE b.provider IS a.provider AND b.model IS a.model AND b.purpose=a.purpose
                 ORDER BY b.ran_at DESC, b.id DESC LIMIT 1) AS last_failed
                FROM judge_runs a GROUP BY provider, model, purpose ORDER BY last_at DESC""",
                (today, week, month_window, month, *[month_window] * 4),
            ).fetchall()
        return [
            JudgeRunUsage(
                row["provider"],
                row["model"],
                row["purpose"],
                row["today"],
                row["last_7_days"],
                row["last_30_days"],
                row["this_month"],
                row["total"],
                row["failures"],
                row["last_at"],
                bool(row["last_failed"]),
                row["measured"],
                int(row["input"]),
                int(row["cached"]),
                int(row["output"]),
            )
            for row in rows
        ]


class RecordingJudge:
    """判定器を包み、judge を呼ぶたびに成否を記録する（判定の結果と例外はそのまま返す）。"""

    def __init__(
        self,
        judge: "VideoJudge",
        log: JudgeRunLog,
        provider: str,
        model: str | None,
        purpose: str,
    ) -> None:
        self.inner = judge
        self.log = log
        self.provider = provider
        self.model = model
        self.purpose = purpose
        self.name = judge.name
        # 直近の呼び出しが失敗したか。まだ呼んでいなければ None。
        self.failed: bool | None = None

    @property
    def last_usage(self) -> TokenUsage | None:
        """包んだ判定器が直近の呼び出しで使ったトークン数（出さない判定器は None）。"""
        usage = getattr(self.inner, "last_usage", None)
        return usage if isinstance(usage, TokenUsage) else None

    def check(self) -> None:
        self.inner.check()

    def judge(
        self, folder: Path, prompt: str, images: Sequence[str], schema: dict[str, Any]
    ) -> object:
        try:
            output = self.inner.judge(folder, prompt, images, schema)
        except BaseException as error:
            self.failed = True
            cause = failure_cause(error)
            # 種類と詳細だけを出す。例外の文言には CLI の出力が入りうる。
            logger.warning(
                "判定器の呼び出しに失敗しました（%s: %s%s）",
                self.provider,
                cause.kind,
                f" {cause.detail}" if cause.detail else "",
            )
            self._record(cause)
            raise
        self.failed = False
        self._record(None)
        return output

    def _record(self, failure: FailureCause | None) -> None:
        # トークン数は、出せる判定器だけが last_usage に置く。
        usage = self.last_usage
        try:
            self.log.record(
                self.provider,
                self.model,
                self.purpose,
                failed=failure is not None,
                usage=usage,
                failure=failure.kind if failure else None,
                failure_detail=failure.detail if failure else None,
            )
        except sqlite3.Error:
            # 使用量の記録が書けなくても、判定そのものは続ける。
            logger.warning("判定器の呼び出しを記録できませんでした（%s）", self.provider)
