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
    is_treat: bool = False
    # 食べた分のカロリーを直接記録するときは0.1 kcal単位で保持する。
    energy_dk: int | None = None

    @property
    def grams(self) -> str:
        return format_grams(self.amount_dg)


def format_grams(amount_dg: int | None) -> str:
    if amount_dg is None:
        return ""
    whole, tenth = divmod(amount_dg, 10)
    return f"{whole}.{tenth}" if tenth else str(whole)


def parse_amount(value: str, *, label: str = "給餌量", unit: str = "g") -> int | None:
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
        raise InvalidInputError(f"{label}は0.1 {unit}単位の正の数で入力してください") from None
    return units


def feeding_history(database: Database) -> tuple[str, str, list[str]]:
    with database.connect() as db:
        food, amount, _ = _last_feeding(db) or ("", None, None)
        names = db.execute(
            """SELECT food FROM care_entries WHERE kind='feeding' AND food != '' AND deleted_at IS NULL
            GROUP BY food ORDER BY MAX(at) DESC, food"""
        ).fetchall()
    return (
        food,
        format_grams(amount),
        [row["food"] for row in names],
    )


def entries(database: Database, day: date, last: date | None = None) -> list[CareEntry]:
    """削除済みを除き、ローカル日付の指定範囲（両端を含む）の記録を返す。"""
    start = datetime.combine(day, time.min).astimezone(UTC).isoformat()
    end = datetime.combine((last or day) + timedelta(days=1), time.min).astimezone(UTC).isoformat()
    with database.connect() as db:
        rows = db.execute(
            """SELECT * FROM care_entries WHERE at >= ? AND at < ? AND deleted_at IS NULL
            ORDER BY at DESC, rowid DESC""",
            (start, end),
        ).fetchall()
    return [_entry(row) for row in rows]


def feeding_totals(database: Database, first: date, last: date) -> dict[date, dict[str, int]]:
    """ローカルの日付ごと・フードごとの給餌量（0.1 g単位）。削除済みと量のない記録は除く。"""
    start = datetime.combine(first, time.min).astimezone(UTC).isoformat()
    end = datetime.combine(last + timedelta(days=1), time.min).astimezone(UTC).isoformat()
    with database.connect() as db:
        rows = db.execute(
            """SELECT at, food, amount_dg FROM care_entries
            WHERE kind='feeding' AND amount_dg IS NOT NULL AND deleted_at IS NULL
            AND at >= ? AND at < ?""",
            (start, end),
        ).fetchall()
    totals: dict[date, dict[str, int]] = {}
    for row in rows:
        foods = totals.setdefault(to_local(row["at"]).date(), {})
        food = row["food"].strip()
        foods[food] = foods.get(food, 0) + row["amount_dg"]
    return totals


def _entry(row: sqlite3.Row) -> CareEntry:
    return CareEntry(
        row["id"],
        to_local(row["at"]),
        row["kind"],
        row["actor"],
        row["note"],
        row["food"],
        row["amount_dg"],
        bool(row["is_treat"]),
        row["energy_dk"],
    )


def latest_entries(database: Database) -> dict[str, CareEntry]:
    """ごはんとシート交換について、実施時刻が最新の記録を返す。"""
    latest = {}
    with database.connect() as db:
        for kind in KINDS:
            row = db.execute(
                """SELECT * FROM care_entries WHERE kind=? AND is_treat=0 AND deleted_at IS NULL
                ORDER BY at DESC, rowid DESC LIMIT 1""",
                (kind,),
            ).fetchone()
            if row is not None:
                latest[kind] = _entry(row)
    return latest


def save_entry(database: Database, entry: CareEntry, *, plan_id: str | None = None) -> str:
    """記録を足すか訂正し、その ID を返す。"""
    with database.connect() as db:
        if plan_id:
            db.execute("BEGIN IMMEDIATE")
        return _save_entry(db, entry, plan_id)


def _save_entry(db: sqlite3.Connection, entry: CareEntry, plan_id: str | None) -> str:
    if entry.kind not in KINDS or entry.at > local_now() or len(entry.note) > 500:
        raise InvalidInputError("種類、日時、メモ（500文字以内）を確認してください")
    food = entry.food.strip() if entry.kind == "feeding" else ""
    amount = entry.amount_dg if entry.kind == "feeding" else None
    is_treat = entry.kind == "feeding" and entry.is_treat
    energy = entry.energy_dk if entry.kind == "feeding" else None
    if energy is not None and energy <= 0:
        raise InvalidInputError("カロリーは正の数で入力してください")
    if len(food) > 120:
        raise InvalidInputError("フードの銘柄は120文字以内で入力してください")
    stamp = entry.at.astimezone(UTC).isoformat()
    plan_day = entry.at.astimezone().date().isoformat()
    if plan_id:
        if entry.kind != "feeding" or entry.id or is_treat:
            raise InvalidInputError("新しいごはんの記録だけを予定に結び付けられます")
        existing = db.execute(
            "SELECT id FROM care_entries WHERE plan_id=? AND plan_day=? AND deleted_at IS NULL",
            (plan_id, plan_day),
        ).fetchone()
        if existing:
            return str(existing["id"])
        if not db.execute(
            "SELECT 1 FROM feeding_plans WHERE id=? AND active=1", (plan_id,)
        ).fetchone():
            raise InvalidInputError("選んだごはんの予定はありません")
    if entry.id:
        planned_meal = entry.kind == "feeding" and not is_treat
        if not db.execute(
            """UPDATE care_entries SET at=?, kind=?, note=?, food=?, amount_dg=?,
            is_treat=?, energy_dk=?,
            plan_id=CASE WHEN ? AND plan_day=? THEN plan_id END,
            plan_day=CASE WHEN ? AND plan_day=? THEN plan_day END
            WHERE id=? AND deleted_at IS NULL""",
            (
                stamp,
                entry.kind,
                entry.note,
                food,
                amount,
                is_treat,
                energy,
                planned_meal,
                plan_day,
                planned_meal,
                plan_day,
                entry.id,
            ),
        ).rowcount:
            raise InvalidInputError("訂正する記録が見つかりません")
        return entry.id
    identifier = uuid4().hex
    db.execute(
        """INSERT INTO care_entries (id, at, kind, actor, note, food, amount_dg, plan_id, plan_day,
        is_treat, energy_dk) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            identifier,
            stamp,
            entry.kind,
            entry.actor,
            entry.note,
            food,
            amount,
            plan_id,
            plan_day if plan_id else None,
            is_treat,
            energy,
        ),
    )
    return identifier


def delete_entry(database: Database, identifier: str) -> date:
    """記録に削除時刻を付け、表示していた日付を返す。再送しても削除時刻は保つ。"""
    with database.connect() as db:
        row = db.execute(
            """UPDATE care_entries SET deleted_at=COALESCE(deleted_at,?)
            WHERE id=? RETURNING at""",
            (datetime.now(UTC).isoformat(), identifier),
        ).fetchone()
        if row is None:
            raise InvalidInputError("削除する記録が見つかりません")
        return to_local(row["at"]).date()


def last_feeding(database: Database) -> tuple[str, int | None] | None:
    """銘柄か量を入れた直近の給餌の (銘柄, 0.1 g単位の量)。なければ None。"""
    with database.connect() as db:
        last = _last_feeding(db)
    return (last[0], last[1]) if last else None


def _last_feeding(db: sqlite3.Connection) -> tuple[str, int | None, int | None] | None:
    row = db.execute(
        """SELECT food, amount_dg, energy_dk FROM care_entries
        WHERE kind='feeding' AND deleted_at IS NULL
        AND is_treat=0 AND (food != '' OR amount_dg IS NOT NULL) ORDER BY at DESC, rowid DESC LIMIT 1"""
    ).fetchone()
    return (row["food"], row["amount_dg"], row["energy_dk"]) if row else None


def feeding_text(food: str, amount_dg: int | None) -> str:
    """給餌の銘柄と量を、画面に出す1つの文字列にする。"""
    amount = f"{format_grams(amount_dg)} g" if amount_dg is not None else ""
    return " ／ ".join(part for part in (food, amount) if part)


def quick_entry(
    database: Database, kind: str, actor: str, *, plan_id: str | None = None
) -> str | None:
    """ホームから1回押して記録する。前回と同じ給餌か、シート交換を、いまの時刻で足す。

    記録の ID を返す。同じ人が同じ種類を DOUBLE_TAP_SECONDS 以内に押したときは、足さずに
    先の記録の ID を返す（押し間違いの二重の記録を防ぐ）。前回の給餌がなければ None。
    """
    if kind not in KINDS:
        raise InvalidInputError("記録する種類を確認してください")
    if plan_id and kind != "feeding":
        raise InvalidInputError("ごはんの予定を指定してください")
    now = local_now()
    with database.connect() as db:
        # 削除とクイック記録の間に、履歴の読み取りと保存が割り込まれないようにする。
        db.execute("BEGIN IMMEDIATE")
        if not plan_id:
            since = (now - timedelta(seconds=DOUBLE_TAP_SECONDS)).astimezone(UTC).isoformat()
            recent = db.execute(
                """SELECT id FROM care_entries WHERE kind=? AND actor=? AND at >= ?
                AND plan_id IS NULL AND is_treat=0 AND deleted_at IS NULL ORDER BY at DESC, rowid DESC LIMIT 1""",
                (kind, actor, since),
            ).fetchone()
            if recent:
                return str(recent["id"])
        last = _last_feeding(db) if kind == "feeding" else None
        if kind == "feeding" and last is None and not plan_id:
            return None
        food, amount, energy = last or ("", None, None)
        entry = CareEntry("", now, kind, actor, "", food, amount, energy_dk=energy)
        return _save_entry(db, entry, plan_id)


def care_time(value: str) -> datetime:
    try:
        return parse_local(value)
    except ValueError:
        raise InvalidInputError("実施した日時を入力してください") from None
