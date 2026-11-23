"""犬が鳴いていた区間（時刻・長さ・大きさだけ。音声は保存しない）を記録する。"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from .database import Database

CLIP_DIR = "vocal-clips"
# 人が付けるラベル（評価用）。
VOCAL_LABELS = {"dog": "鳴き声", "other": "ほかの音"}


@dataclass(frozen=True)
class VocalEpisode:
    """鳴いていた1区間。loud_seconds は鳴いていた時間の合計、peak_db は最も大きな音量（dBFS）。

    dog_score・top_label は鳴き声のモデル（audio.model）の犬らしさといちばん近いクラス、
    label は人のラベル（dog・other）。
    """

    started_at: datetime
    ended_at: datetime
    loud_seconds: float
    peak_db: float
    id: int = 0
    dog_score: float | None = None
    top_label: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class VocalEvaluation:
    """人のラベルと比べた、今の判断（帯域の割合）とモデルの当たり具合。

    今の判断は記録した区間をすべて鳴き声と数えるので、見逃しは測れず、適合率だけを出す。
    モデルの値は、犬らしさのある区間だけで数える（なければ None）。
    """

    labeled: int
    dogs: int
    band_precision: float | None
    model_cases: int
    model_precision: float | None
    model_recall: float | None


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class VocalEpisodes:
    """vocal_episodes テーブル。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self,
        started: datetime,
        ended: datetime,
        loud_seconds: float,
        peak_db: float,
        *,
        dog_score: float | None = None,
        top_label: str | None = None,
    ) -> int:
        with self.database.connect() as db:
            cursor = db.execute(
                "INSERT INTO vocal_episodes "
                "(started_at, ended_at, loud_seconds, peak_db, dog_score, top_label) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_stamp(started), _stamp(ended), loud_seconds, peak_db, dog_score, top_label),
            )
        return int(cursor.lastrowid or 0)

    @staticmethod
    def clip_path(root: str | Path, identifier: int) -> Path:
        """評価用に残した区間の音（audio.keep_days のときだけある）。"""
        return Path(root) / CLIP_DIR / f"{int(identifier)}.wav"

    def label(self, identifier: int, label: str, user: str) -> None:
        if label not in VOCAL_LABELS:
            raise ValueError(label)
        with self.database.connect() as db:
            updated = db.execute(
                "UPDATE vocal_episodes SET label=?, labeled_by=?, labeled_at=? WHERE id=?",
                (label, user, _stamp(datetime.now().astimezone()), identifier),
            ).rowcount
        if not updated:
            raise KeyError(identifier)

    def evaluation(self, threshold: float) -> VocalEvaluation:
        """人のラベルのある区間で、今の判断と、犬らしさ threshold 以上をモデルの鳴き声とした場合を比べる。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT label, dog_score FROM vocal_episodes WHERE label IS NOT NULL"
            ).fetchall()
        dogs = sum(row["label"] == "dog" for row in rows)
        scored = [row for row in rows if row["dog_score"] is not None]
        predicted = [row for row in scored if row["dog_score"] >= threshold]
        hits = sum(row["label"] == "dog" for row in predicted)
        scored_dogs = sum(row["label"] == "dog" for row in scored)
        return VocalEvaluation(
            len(rows),
            dogs,
            dogs / len(rows) if rows else None,
            len(scored),
            hits / len(predicted) if predicted else None,
            hits / scored_dogs if scored_dogs else None,
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
        int(row["id"]),
        row["dog_score"],
        row["top_label"],
        row["label"],
    )
