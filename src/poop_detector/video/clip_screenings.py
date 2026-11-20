"""判定の前のふるい分けの指標（clip_screenings テーブル）。記録するだけで、判定には使わない。"""

import json
from collections.abc import Collection
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from ..records.database import Database

if TYPE_CHECKING:
    from ..experimental.screening import Screening
    from .clips import ClipRow

# 指標の列。experimental.screening.Screening の項目と同じ順に並べる。
COLUMNS = (
    "sampled",
    "tray_frames",
    "weak_tray_frames",
    "tray_seconds",
    "weak_tray_seconds",
    "unaligned",
    "located",
    "net_turning",
    "path_length",
    "stop_seconds",
)


class ScreeningStore:
    """clip_screenings テーブル。動画1本につき、最後に求めた指標を1行持つ。"""

    def __init__(self, database: Database) -> None:
        self.connect = database.connect

    def save(self, clip_id: str, screening: "Screening", version: int | None = None) -> bool:
        """指標を保存する。変換済みの動画がなければ保存せず False を返す。"""
        from ..experimental.screening import SCREENING_VERSION

        values = asdict(screening)
        placeholders = ", ".join("?" for _ in COLUMNS)
        updates = ", ".join(f"{column}=excluded.{column}" for column in COLUMNS)
        with self.connect() as db:
            cursor = db.execute(
                f"""INSERT INTO clip_screenings (clip_id, version, screened_at, {", ".join(COLUMNS)})
                SELECT id, ?, ?, {placeholders} FROM clips WHERE id=? AND status='ready'
                ON CONFLICT(clip_id) DO UPDATE SET version=excluded.version,
                screened_at=excluded.screened_at, {updates}""",  # noqa: S608
                (
                    SCREENING_VERSION if version is None else version,
                    datetime.now(UTC).isoformat(),
                    *(values[column] for column in COLUMNS),
                    clip_id,
                ),
            )
            return cursor.rowcount == 1

    def get(self, clip_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM clip_screenings WHERE clip_id=?", (clip_id,)).fetchone()
        return dict(row) if row else None

    def targets(
        self, signatures: Collection[str], limit: int, clip_id: str | None = None
    ) -> list["ClipRow"]:
        """指標を求める、signatures のどれかの画角で録った変換済みの動画（新しい順）。

        clip_id を指定すると、記録済みでも返す（求め直す）。
        """
        query = """SELECT c.* FROM clips c LEFT JOIN clip_screenings s ON s.clip_id=c.id
            WHERE c.status='ready' AND c.interrupted=0
            AND c.scene_signature IN (SELECT value FROM json_each(?))"""
        params: list[object] = [json.dumps(sorted(signatures))]
        if clip_id is None:
            query += " AND s.clip_id IS NULL"
        else:
            query += " AND c.id=?"
            params.append(clip_id)
        with self.connect() as db:
            rows = db.execute(query + " ORDER BY c.requested_at DESC LIMIT ?", (*params, limit))
            return [cast("ClipRow", dict(row)) for row in rows]
