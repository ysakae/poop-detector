"""犬が鳴いていた区間（時刻・長さ・大きさだけ。音声は保存しない）を記録する。"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from .database import Database


@dataclass(frozen=True)
class VocalEpisode:
    """鳴いていた1区間。loud_seconds は鳴いていた時間の合計、peak_db は最も大きな音量（dBFS）。"""

    started_at: datetime
    ended_at: datetime
    loud_seconds: float
    peak_db: float


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class VocalEpisodes:
    """vocal_episodes テーブル。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, started: datetime, ended: datetime, loud_seconds: float, peak_db: float) -> None:
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO vocal_episodes (started_at, ended_at, loud_seconds, peak_db) "
                "VALUES (?, ?, ?, ?)",
                (_stamp(started), _stamp(ended), loud_seconds, peak_db),
            )

    def between(self, start: datetime, end: datetime) -> list[VocalEpisode]:
        """start <= 始まり <= end の区間を時刻の順に返す。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM vocal_episodes WHERE started_at >= ? AND started_at <= ? "
                "ORDER BY started_at",
                (_stamp(start), _stamp(end)),
            ).fetchall()
        return [_episode(row) for row in rows]

    def list(self, limit: int = 50) -> list[VocalEpisode]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM vocal_episodes ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_episode(row) for row in rows]

    def daily_totals(self, first: date, last: date) -> dict[date, tuple[int, float]]:
        """ローカル時刻の日ごとの（区間の数, 鳴いていた秒数）。区間のない日は入れない。"""
        start = datetime.combine(first, time.min).astimezone()
        end = datetime.combine(last + timedelta(days=1), time.min).astimezone()
        totals: dict[date, tuple[int, float]] = {}
        for episode in self.between(start, end - timedelta(microseconds=1)):
            day = episode.started_at.astimezone().date()
            count, seconds = totals.get(day, (0, 0.0))
            totals[day] = (count + 1, seconds + episode.loud_seconds)
        return totals


def _episode(row: sqlite3.Row) -> VocalEpisode:
    return VocalEpisode(
        datetime.fromisoformat(row["started_at"]),
        datetime.fromisoformat(row["ended_at"]),
        float(row["loud_seconds"]),
        float(row["peak_db"]),
    )
