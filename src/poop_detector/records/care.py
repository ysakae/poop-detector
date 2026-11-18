"""給餌とシート交換を、家族で共有する記録に残す。"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from ..errors import InvalidInputError
from ..timezones import local_now, parse_local, to_local
from .database import Database

KINDS = {"feeding": "給餌", "sheet": "シート交換"}
# ホームからの記録で、同じ人の同じ種類の押し直しを二重の記録にしない秒数。
DOUBLE_TAP_SECONDS = 30


@dataclass(frozen=True)
class CareEntry:
    """お世話の種類・時刻・担当者・メモ。"""

    id: str
    at: datetime
    kind: str
    actor: str
    note: str
    food: str = ""
    # 0.1 g単位の整数で保持する。
    amount_dg: int | None = None

    @property
    def grams(self) -> str:
        return format_grams(self.amount_dg)


def format_grams(amount_dg: int | None) -> str:
    if amount_dg is None:
        return ""
    whole, tenth = divmod(amount_dg, 10)
    return f"{whole}.{tenth}" if tenth else str(whole)


def parse_amount(value: str) -> int | None:
    value = value.strip().replace(",", ".")
    if not value:
        return None
    try:
        if not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9])?", value):
            raise InvalidOperation
        units = int(Decimal(value) * 10)
        if units <= 0:
            raise InvalidOperation
    except InvalidOperation:
        raise InvalidInputError("給餌量は0.1 g単位の正の数で入力してください") from None
    return units


def feeding_history(database: Database) -> tuple[str, str, list[str]]:
    with database.connect() as db:
        last = db.execute(
            """SELECT food, amount_dg FROM care_entries WHERE kind='feeding'
            AND (food != '' OR amount_dg IS NOT NULL) ORDER BY at DESC, rowid DESC LIMIT 1"""
        ).fetchone()
        names = db.execute(
            """SELECT food FROM care_entries WHERE kind='feeding' AND food != ''
            GROUP BY food ORDER BY MAX(at) DESC, food"""
        ).fetchall()
    return (
        last["food"] if last else "",
        format_grams(last["amount_dg"]) if last else "",
        [row["food"] for row in names],
    )


def entries(database: Database, day: date) -> list[CareEntry]:
    start = datetime.combine(day, time.min).astimezone(UTC).isoformat()
    end = datetime.combine(day + timedelta(days=1), time.min).astimezone(UTC).isoformat()
    with database.connect() as db:
        rows = db.execute(
            "SELECT * FROM care_entries WHERE at >= ? AND at < ? ORDER BY at DESC, rowid DESC",
            (start, end),
        ).fetchall()
    return [_entry(row) for row in rows]


def _entry(row: sqlite3.Row) -> CareEntry:
    return CareEntry(
        row["id"],
        to_local(row["at"]),
        row["kind"],
        row["actor"],
        row["note"],
        row["food"],
        row["amount_dg"],
    )


def latest_entries(database: Database) -> dict[str, CareEntry]:
    """ごはんとシート交換について、実施時刻が最新の記録を返す。"""
    latest = {}
    with database.connect() as db:
        for kind in KINDS:
            row = db.execute(
                "SELECT * FROM care_entries WHERE kind=? ORDER BY at DESC, rowid DESC LIMIT 1",
                (kind,),
            ).fetchone()
            if row is not None:
                latest[kind] = _entry(row)
    return latest


def save_entry(database: Database, entry: CareEntry) -> str:
    """記録を足すか訂正し、その ID を返す。"""
    if entry.kind not in KINDS or entry.at > local_now() or len(entry.note) > 500:
        raise InvalidInputError("種類、日時、メモ（500文字以内）を確認してください")
    food = entry.food.strip() if entry.kind == "feeding" else ""
    amount = entry.amount_dg if entry.kind == "feeding" else None
    if len(food) > 120:
        raise InvalidInputError("フードの銘柄は120文字以内で入力してください")
    stamp = entry.at.astimezone(UTC).isoformat()
    with database.connect() as db:
        if entry.id:
            if not db.execute(
                "UPDATE care_entries SET at=?, kind=?, note=?, food=?, amount_dg=? WHERE id=?",
                (stamp, entry.kind, entry.note, food, amount, entry.id),
            ).rowcount:
                raise InvalidInputError("訂正する記録が見つかりません")
            return entry.id
        identifier = uuid4().hex
        db.execute(
            """INSERT INTO care_entries (id, at, kind, actor, note, food, amount_dg)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (identifier, stamp, entry.kind, entry.actor, entry.note, food, amount),
        )
    return identifier


def last_feeding(database: Database) -> tuple[str, int | None] | None:
    """銘柄か量を入れた直近の給餌の (銘柄, 0.1 g単位の量)。なければ None。"""
    with database.connect() as db:
        row = db.execute(
            """SELECT food, amount_dg FROM care_entries WHERE kind='feeding'
            AND (food != '' OR amount_dg IS NOT NULL) ORDER BY at DESC, rowid DESC LIMIT 1"""
        ).fetchone()
    return (row["food"], row["amount_dg"]) if row else None


def feeding_text(food: str, amount_dg: int | None) -> str:
    """給餌の銘柄と量を、画面に出す1つの文字列にする。"""
    amount = f"{format_grams(amount_dg)} g" if amount_dg is not None else ""
    return " ／ ".join(part for part in (food, amount) if part)


def quick_entry(database: Database, kind: str, actor: str) -> str | None:
    """ホームから1回押して記録する。前回と同じ給餌か、シート交換を、いまの時刻で足す。

    記録の ID を返す。同じ人が同じ種類を DOUBLE_TAP_SECONDS 以内に押したときは、足さずに
    先の記録の ID を返す（押し間違いの二重の記録を防ぐ）。前回の給餌がなければ None。
    """
    if kind not in KINDS:
        raise InvalidInputError("記録する種類を確認してください")
    now = local_now()
    since = (now - timedelta(seconds=DOUBLE_TAP_SECONDS)).astimezone(UTC).isoformat()
    with database.connect() as db:
        recent = db.execute(
            """SELECT id FROM care_entries WHERE kind=? AND actor=? AND at >= ?
            ORDER BY at DESC, rowid DESC LIMIT 1""",
            (kind, actor, since),
        ).fetchone()
    if recent:
        return str(recent["id"])
    if kind == "feeding":
        last = last_feeding(database)
        if last is None:
            return None
        return save_entry(database, CareEntry("", now, kind, actor, "", last[0], last[1]))
    return save_entry(database, CareEntry("", now, kind, actor, ""))


def care_time(value: str) -> datetime:
    try:
        return parse_local(value)
    except ValueError:
        raise InvalidInputError("実施した日時を入力してください") from None
