"""留守中のまとめの、判定器による短い文章の要約（外出の区間ごとに1件）を保存する。"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .database import Database
from .events import utcnow

# 要約の作成がこの秒数を過ぎても終わらなければ、止まったとみなす。
STALE_SECONDS = 900


@dataclass(frozen=True)
class DigestSummary:
    """要約の状態（queued・running・done・failed）と、できた要約・気になる点。"""

    period_id: str
    status: str
    summary: str
    points: list[str]
    failure: str | None


def _summary(row: sqlite3.Row) -> DigestSummary:
    return DigestSummary(
        row["period_id"],
        row["status"],
        row["summary"] or "",
        json.loads(row["points"] or "[]"),
        row["failure"],
    )


class DigestSummaries:
    """digest_summaries テーブル。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, period_id: str) -> DigestSummary | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM digest_summaries WHERE period_id=?", (period_id,)
            ).fetchone()
        return _summary(row) if row else None

    def request(self, period_id: str) -> None:
        """要約を作る（作り直す）よう頼む。作っている途中なら何もしない。"""
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO digest_summaries (period_id, status, requested_at) "
                "VALUES (?, 'queued', ?) ON CONFLICT(period_id) DO UPDATE SET "
                "status='queued', requested_at=excluded.requested_at, failure=NULL "
                "WHERE status != 'running'",
                (period_id, utcnow()),
            )

    def claim(self, since: datetime) -> str | None:
        """since 以降に終わった外出のうち、まだ要約がないものは頼んだことにし、
        頼まれている要約を古い順に1件 running にして、その区間の ID を返す。

        取り出しは BEGIN IMMEDIATE の中で行い、同じ要約を2つの処理が取らない。
        """
        stale = (datetime.now(UTC) - timedelta(seconds=STALE_SECONDS)).isoformat()
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE digest_summaries SET status='failed', failure='timeout' "
                "WHERE status='running' AND started_at < ?",
                (stale,),
            )
            db.execute(
                "INSERT OR IGNORE INTO digest_summaries (period_id, status, requested_at) "
                "SELECT id, 'queued', ? FROM away_periods "
                "WHERE ended_at IS NOT NULL AND ended_at >= ?",
                (utcnow(), since.astimezone(UTC).isoformat()),
            )
            row = db.execute(
                "SELECT period_id FROM digest_summaries WHERE status='queued' "
                "ORDER BY requested_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE digest_summaries SET status='running', started_at=? WHERE period_id=?",
                (utcnow(), row["period_id"]),
            )
        return str(row["period_id"])

    def finish(
        self, period_id: str, summary: str, points: list[str], provider: str, model: str | None
    ) -> None:
        with self.database.connect() as db:
            db.execute(
                "UPDATE digest_summaries SET status='done', summary=?, points=?, provider=?, "
                "model=?, finished_at=?, failure=NULL WHERE period_id=?",
                (
                    summary,
                    json.dumps(points, ensure_ascii=False),
                    provider,
                    model,
                    utcnow(),
                    period_id,
                ),
            )

    def fail(self, period_id: str, failure: str) -> None:
        with self.database.connect() as db:
            db.execute(
                "UPDATE digest_summaries SET status='failed', failure=?, finished_at=? "
                "WHERE period_id=?",
                (failure, utcnow(), period_id),
            )
