"""犬の生年月日と体重を、排泄記録と同じデータベースに保存する。"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from ..errors import InvalidInputError
from ..timezones import local_today
from .database import Database


@dataclass(frozen=True)
class Weight:
    """測定日の体重（グラム）とメモ。"""

    id: str
    day: date
    grams: int
    note: str


def parse_day(value: str) -> date:
    try:
        day = date.fromisoformat(value)
        if day <= local_today() and day.isoformat() == value:
            return day
    except ValueError:
        pass
    raise InvalidInputError("今日以前の日付を指定してください")


def parse_grams(value: str, unit: str) -> int:
    if unit not in ("g", "kg") or len(value) > 24:
        raise InvalidInputError("体重と単位を確認してください")
    try:
        weight = Decimal(value)
        if not weight.is_finite() or weight <= 0 or weight > (200 if unit == "kg" else 200000):
            raise InvalidOperation
        grams = weight * (1000 if unit == "kg" else 1)
        if grams != grams.to_integral_value():
            raise InvalidOperation
    except InvalidOperation:
        raise InvalidInputError("体重は1 gから200 kgまで、1 g単位で入力してください") from None
    return int(grams)


def birth_date(database: Database) -> date | None:
    with database.connect() as db:
        row = db.execute("SELECT birth FROM pet_profile WHERE id = 1").fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def save_birth_date(database: Database, birth: str) -> None:
    value = parse_day(birth).isoformat() if birth else None
    with database.connect() as db:
        db.execute(
            "INSERT INTO pet_profile VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET birth=excluded.birth",
            (value,),
        )


def weights(database: Database, start: date = date.min, end: date = date.max) -> list[Weight]:
    with database.connect() as db:
        rows = db.execute(
            "SELECT * FROM weights WHERE day BETWEEN ? AND ? ORDER BY day, rowid",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [
        Weight(row["id"], date.fromisoformat(row["day"]), row["grams"], row["note"]) for row in rows
    ]


def save_weight(database: Database, identifier: str, day: date, grams: int, note: str) -> None:
    if len(note) > 500:
        raise InvalidInputError("メモは500文字以内にしてください")
    with database.connect() as db:
        if not identifier:
            db.execute(
                "INSERT INTO weights VALUES (?, ?, ?, ?)",
                (uuid4().hex, day.isoformat(), grams, note),
            )
        elif not db.execute(
            "UPDATE weights SET day=?, grams=?, note=? WHERE id=?",
            (day.isoformat(), grams, note, identifier),
        ).rowcount:
            raise InvalidInputError("訂正する体重記録が見つかりません")
