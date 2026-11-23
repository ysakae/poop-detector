"""家族が「外出中」にしていた区間（留守中のダイジェストの単位）を保存する。"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from .database import Database
from .events import utcnow


@dataclass(frozen=True)
class AwayPeriod:
    """外出中の1区間。ended_at が None なら外出中。"""

    id: str
    started_at: datetime
    ended_at: datetime | None
    # 在宅に戻し忘れを知らせたか（features.away_reminder）。
    reminded: bool = False


def _period(row: sqlite3.Row) -> AwayPeriod:
    ended = row["ended_at"]
    return AwayPeriod(
        row["id"],
        datetime.fromisoformat(row["started_at"]),
        datetime.fromisoformat(ended) if ended else None,
        row["reminded_at"] is not None,
    )


class AwayPeriods:
    """away_periods テーブル。外出中の切り替え（records.absences.AwayMode）が書く。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def current(self) -> AwayPeriod | None:
        with self.database.connect() as db:
            return _open(db)

    def get(self, identifier: str) -> AwayPeriod | None:
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM away_periods WHERE id=?", (identifier,)).fetchone()
        return _period(row) if row else None

    def list(self, limit: int = 30) -> list[AwayPeriod]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM away_periods ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_period(row) for row in rows]

    def mark_reminded(self, identifier: str) -> None:
        with self.database.connect() as db:
            db.execute("UPDATE away_periods SET reminded_at=? WHERE id=?", (utcnow(), identifier))

    def latest_finished(self) -> AwayPeriod | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM away_periods WHERE ended_at IS NOT NULL "
                "ORDER BY ended_at DESC LIMIT 1"
            ).fetchone()
        return _period(row) if row else None


def _open(db: sqlite3.Connection) -> AwayPeriod | None:
    row = db.execute(
        "SELECT * FROM away_periods WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return _period(row) if row else None


def switch(db: sqlite3.Connection, away: bool, user: str) -> None:
    """外出中の切り替えと同じトランザクションで、区間を始めるか閉じる（同じ状態なら何もしない）。"""
    current = _open(db)
    if away and current is None:
        db.execute(
            "INSERT INTO away_periods (id, started_at, started_by) VALUES (?, ?, ?)",
            (uuid4().hex, utcnow(), user),
        )
    elif not away and current is not None:
        db.execute(
            "UPDATE away_periods SET ended_at=?, ended_by=? WHERE id=?",
            (utcnow(), user, current.id),
        )
