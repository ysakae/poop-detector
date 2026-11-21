"""フードのカロリーの台帳と、AI へのカロリー探しの依頼を管理する。"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from ..errors import InvalidInputError
from ..timezones import local_now
from .care import feeding_history
from .database import Database

KCAL_ERROR = "カロリーは30〜700 kcal/100gの範囲で、0.1単位で入力してください"
URL_ERROR = "出典はhttpsのURLで入力してください"
MAX_URL_LENGTH = 500
MAX_NAME_LENGTH = 80


@dataclass(frozen=True)
class Food:
    """台帳に載せたフードの名前とカロリー（100 gあたり、0.1 kcal単位の整数）。"""

    id: str
    name: str
    kcal_dk: int
    source_url: str


@dataclass(frozen=True)
class Lookup:
    """AI へのカロリー探しの依頼と、その結果。"""

    id: str
    name: str
    status: str
    product_name: str
    maker: str
    kcal_dk: int | None
    source_url: str
    note: str
    failure: str | None


def parse_kcal(value: str) -> int:
    """100 gあたりの kcal（30.0〜700.0、0.1単位）を 0.1 kcal単位の整数にする。"""
    text = value.strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9])?", text, re.ASCII):
        raise InvalidInputError(KCAL_ERROR)
    kcal = Decimal(text)
    if not Decimal(30) <= kcal <= Decimal(700):
        raise InvalidInputError(KCAL_ERROR)
    return int(kcal * 10)


def parse_source_url(value: str) -> str:
    """空か、空白・制御文字のない500文字以内の https の URL だけを通す。"""
    text = value
    if not text:
        return ""
    if (
        not text.startswith("https://")
        or len(text) > MAX_URL_LENGTH
        or any(char.isspace() or not char.isprintable() for char in text)
    ):
        raise InvalidInputError(URL_ERROR)
    return text


def save_food(
    database: Database,
    *,
    name: str,
    kcal_dk: int,
    source_url: str,
    user_id: str,
    food_id: str = "",
    now: datetime | None = None,
) -> str:
    name = name.strip()
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise InvalidInputError(f"フードの名前は1〜{MAX_NAME_LENGTH}文字で入力してください")
    stamp = (now or local_now()).astimezone(UTC).isoformat()
    try:
        with database.connect() as db:
            if food_id:
                changed = db.execute(
                    "UPDATE foods SET name=?, kcal_dk=?, source_url=? WHERE id=? AND active=1",
                    (name, kcal_dk, source_url, food_id),
                ).rowcount
                if not changed:
                    raise InvalidInputError("変更するフードが見つかりません")
                return food_id
            food_id = uuid4().hex
            db.execute(
                """INSERT INTO foods (id, name, kcal_dk, source_url, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (food_id, name, kcal_dk, source_url, user_id, stamp),
            )
    except sqlite3.IntegrityError:
        raise InvalidInputError("同じ名前のフードがすでにあります") from None
    return food_id


def stop_food(database: Database, food_id: str) -> None:
    with database.connect() as db:
        changed = db.execute("UPDATE foods SET active=0 WHERE id=?", (food_id,)).rowcount
        if not changed:
            raise InvalidInputError("停止するフードが見つかりません")


def active_foods(database: Database) -> list[Food]:
    with database.connect() as db:
        rows = db.execute(
            "SELECT id, name, kcal_dk, source_url FROM foods WHERE active=1 ORDER BY name"
        ).fetchall()
    return [Food(row["id"], row["name"], row["kcal_dk"], row["source_url"]) for row in rows]


def kcal_by_name(database: Database) -> dict[str, int]:
    return {food.name: food.kcal_dk for food in active_foods(database)}


def profile_kcal(database: Database, food_id: str | None) -> int | None:
    """使用中のフードのカロリー。止めたフードや未指定は None（フードなしとして扱う）。"""
    if not food_id:
        return None
    with database.connect() as db:
        row = db.execute("SELECT kcal_dk FROM foods WHERE id=? AND active=1", (food_id,)).fetchone()
    return None if row is None else int(row["kcal_dk"])


def request_lookup(database: Database, name: str, user_id: str, now: datetime | None = None) -> str:
    """カロリー探しを依頼する。同じ名前の待ち・実行中があれば、その ID を返す。"""
    name = name.strip()
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise InvalidInputError(f"フードの名前は1〜{MAX_NAME_LENGTH}文字で入力してください")
    stamp = (now or local_now()).astimezone(UTC).isoformat()
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT id FROM food_lookups WHERE name=? AND status IN ('queued', 'running')",
            (name,),
        ).fetchone()
        if row is not None:
            return str(row["id"])
        lookup_id = uuid4().hex
        db.execute(
            """INSERT INTO food_lookups (id, name, status, requested_by, requested_at)
            VALUES (?, ?, 'queued', ?, ?)""",
            (lookup_id, name, user_id, stamp),
        )
    return lookup_id


def recent_lookups(database: Database, limit: int = 10) -> list[Lookup]:
    with database.connect() as db:
        rows = db.execute(
            "SELECT * FROM food_lookups ORDER BY requested_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        Lookup(
            row["id"],
            row["name"],
            row["status"],
            row["product_name"] or "",
            row["maker"] or "",
            row["kcal_dk"],
            row["source_url"] or "",
            row["note"] or "",
            row["failure"],
        )
        for row in rows
    ]


def unregistered_names(database: Database) -> list[str]:
    """給餌の記録にあって台帳にないフードの名前（新しい記録の順）。"""
    registered = kcal_by_name(database)
    return [name for name in feeding_history(database)[2] if name not in registered]
