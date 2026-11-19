"""犬の睡眠の記録（sleep_minutes・sleep_detection_hours テーブル）の読み書き。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, get_args

from ..records.database import Database

SleepState = Literal["asleep", "awake", "absent", "unknown"]


@dataclass(frozen=True)
class MinuteRecord:
    """1分間の状態。minute はタイムゾーン付きで、分の始まり。"""

    minute: datetime
    state: SleepState
    observed_seconds: float


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class SleepStore:
    """分ごとの睡眠の状態と、時ごとの検出の統計を SQLite に保存する。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def write_minutes(self, records: Sequence[MinuteRecord]) -> None:
        """分の記録を書く。同じ分があれば上書きする。"""
        valid = get_args(SleepState)
        for record in records:
            if record.state not in valid:
                raise ValueError(f"睡眠の状態が不正です: {record.state!r}")
        with self.database.connect() as db:
            db.executemany(
                """INSERT INTO sleep_minutes (minute, state, observed_seconds)
                VALUES (?, ?, ?)
                ON CONFLICT(minute) DO UPDATE SET
                    state=excluded.state, observed_seconds=excluded.observed_seconds""",
                [(_stamp(r.minute), r.state, r.observed_seconds) for r in records],
            )

    def add_detection(
        self, hour: datetime, present_seconds: float, detected_seconds: float
    ) -> None:
        """時の始まりごとに、在室の秒数と犬を検出できた秒数を加算する。"""
        with self.database.connect() as db:
            db.execute(
                """INSERT INTO sleep_detection_hours (hour, present_seconds, detected_seconds)
                VALUES (?, ?, ?)
                ON CONFLICT(hour) DO UPDATE SET
                    present_seconds=present_seconds + excluded.present_seconds,
                    detected_seconds=detected_seconds + excluded.detected_seconds""",
                (_stamp(hour), present_seconds, detected_seconds),
            )

    def minutes(self, start: datetime, end: datetime) -> list[MinuteRecord]:
        """start <= minute < end の記録を時刻順に返す。"""
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT minute, state, observed_seconds FROM sleep_minutes
                WHERE minute >= ? AND minute < ? ORDER BY minute""",
                (_stamp(start), _stamp(end)),
            ).fetchall()
        return [MinuteRecord(datetime.fromisoformat(m), s, float(o)) for m, s, o in rows]

    def detection_hours(
        self, start: datetime, end: datetime
    ) -> dict[datetime, tuple[float, float]]:
        """start <= hour < end の、時ごとの（在室秒, 検出秒）。"""
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT hour, present_seconds, detected_seconds FROM sleep_detection_hours
                WHERE hour >= ? AND hour < ?""",
                (_stamp(start), _stamp(end)),
            ).fetchall()
        return {datetime.fromisoformat(h): (float(p), float(d)) for h, p, d in rows}
