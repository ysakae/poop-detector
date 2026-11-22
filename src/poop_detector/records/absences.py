"""犬がサークルにいなかった区間（不在）と、家族の「外出中」の切り替えを保存する。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from .database import Database
from .events import utcnow

# 不在の区間に人が付けるラベル。
ABSENCE_LABELS = {
    "walk": "散歩",
    "out": "家族と外出・別の部屋",
    "escape": "脱走",
    "false": "誤り（サークルにいた）",
}


@dataclass(frozen=True)
class Absence:
    """不在の1区間。ended_at が None なら続いている。away は外出中に始まったか。"""

    id: str
    started_at: datetime
    ended_at: datetime | None
    away: bool
    interrupted: bool
    label: str | None
    notified: bool

    @property
    def minutes(self) -> float | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds() / 60


class Absences:
    """absences テーブル。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def start(self, at: datetime, *, away: bool) -> str:
        identifier = uuid4().hex
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO absences (id, started_at, away) VALUES (?, ?, ?)",
                (identifier, at.isoformat(), int(away)),
            )
        return identifier

    def end(self, identifier: str, at: datetime, *, interrupted: bool = False) -> None:
        with self.database.connect() as db:
            db.execute(
                "UPDATE absences SET ended_at=?, interrupted=? WHERE id=?",
                (at.isoformat(), int(interrupted), identifier),
            )

    def mark_notified(self, identifier: str) -> None:
        with self.database.connect() as db:
            db.execute("UPDATE absences SET notified_at=? WHERE id=?", (utcnow(), identifier))

    def label(self, identifier: str, label: str, user: str) -> None:
        if label not in ABSENCE_LABELS:
            raise ValueError(label)
        with self.database.connect() as db:
            updated = db.execute(
                "UPDATE absences SET label=?, labeled_at=?, labeled_by=? WHERE id=?",
                (label, utcnow(), user, identifier),
            ).rowcount
        if not updated:
            raise KeyError(identifier)

    def list(self, limit: int = 50) -> list[Absence]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM absences ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Absence(
                row["id"],
                datetime.fromisoformat(row["started_at"]),
                datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
                bool(row["away"]),
                bool(row["interrupted"]),
                row["label"],
                row["notified_at"] is not None,
            )
            for row in rows
        ]


class AwayMode:
    """家族の「外出中」の切り替え（away_mode テーブルの1行）。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def since(self) -> datetime | None:
        """外出中ならその始まりの時刻、在宅なら None。"""
        with self.database.connect() as db:
            row = db.execute("SELECT away_since FROM away_mode WHERE id = 1").fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def set_away(self, away: bool, user: str = "") -> None:
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO away_mode (id, away_since, changed_by) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET away_since=excluded.away_since, "
                "changed_by=excluded.changed_by",
                (utcnow() if away else None, user),
            )
