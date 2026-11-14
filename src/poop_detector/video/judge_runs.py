"""判定器を呼んだ記録（judge_runs テーブル）と、期間別の回数の集計。

判定の台帳（video_assessments）は動画1本につき1行で、失敗した判定は再試行で上書きされる。
使用量は CLI を呼んだ回数で数えたいので、呼ぶたびに1行を追記する別の表に分けている。
記録するのは判定器・モデル・用途・時刻・成否だけで、CLI の応答や画像は入れない。
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..records.database import Database

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
    ) -> None:
        if purpose not in PURPOSES:
            raise ValueError(f"判定器の用途は {'、'.join(PURPOSES)} のいずれかです")
        stamp = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO judge_runs (provider, model, purpose, ran_at, failed)"
                " VALUES (?, ?, ?, ?, ?)",
                (provider, model, purpose, stamp, int(failed)),
            )

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
                (SELECT b.failed FROM judge_runs b
                 WHERE b.provider IS a.provider AND b.model IS a.model AND b.purpose=a.purpose
                 ORDER BY b.ran_at DESC, b.id DESC LIMIT 1) AS last_failed
                FROM judge_runs a GROUP BY provider, model, purpose ORDER BY last_at DESC""",
                (today, week, month_window, month),
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

    def check(self) -> None:
        self.inner.check()

    def judge(
        self, folder: Path, prompt: str, images: Sequence[str], schema: dict[str, Any]
    ) -> object:
        try:
            output = self.inner.judge(folder, prompt, images, schema)
        except BaseException:
            self._record(failed=True)
            raise
        self._record(failed=False)
        return output

    def _record(self, *, failed: bool) -> None:
        try:
            self.log.record(self.provider, self.model, self.purpose, failed=failed)
        except sqlite3.Error:
            # 使用量の記録が書けなくても、判定そのものは続ける。
            logger.warning("判定器の呼び出しを記録できませんでした（%s）", self.provider)
