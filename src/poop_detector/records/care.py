"""給餌とシート交換を、家族で共有する記録に残す。"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

from ..errors import InvalidInputError
from ..timezones import local_now, parse_local, to_local
from .database import Database

KINDS = {"feeding": "給餌", "sheet": "シート交換"}


@dataclass(frozen=True)
class CareEntry:
    """お世話の種類・時刻・担当者・メモ。"""

    id: str
    at: datetime
    kind: str
    actor: str
    note: str


def entries(database: Database, day: date) -> list[CareEntry]:
    start = datetime.combine(day, time.min).astimezone(UTC).isoformat()
    end = datetime.combine(day + timedelta(days=1), time.min).astimezone(UTC).isoformat()
    with database.connect() as db:
        rows = db.execute(
            "SELECT * FROM care_entries WHERE at >= ? AND at < ? ORDER BY at DESC, rowid DESC",
            (start, end),
        ).fetchall()
    return [
        CareEntry(row["id"], to_local(row["at"]), row["kind"], row["actor"], row["note"])
        for row in rows
    ]


def save_entry(database: Database, entry: CareEntry) -> None:
    if entry.kind not in KINDS or entry.at > local_now() or len(entry.note) > 500:
        raise InvalidInputError("種類、日時、メモ（500文字以内）を確認してください")
    stamp = entry.at.astimezone(UTC).isoformat()
    with database.connect() as db:
        if entry.id:
            if not db.execute(
                "UPDATE care_entries SET at=?, kind=?, note=? WHERE id=?",
                (stamp, entry.kind, entry.note, entry.id),
            ).rowcount:
                raise InvalidInputError("訂正する記録が見つかりません")
        else:
            db.execute(
                "INSERT INTO care_entries VALUES (?, ?, ?, ?, ?)",
                (uuid4().hex, stamp, entry.kind, entry.actor, entry.note),
            )


def care_time(value: str) -> datetime:
    try:
        return parse_local(value)
    except ValueError:
        raise InvalidInputError("実施した日時を入力してください") from None
