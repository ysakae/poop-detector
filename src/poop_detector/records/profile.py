"""犬のプロフィール（生年月日・犬種・体格・去勢・フード）の読み書き。"""

import unicodedata
from dataclasses import dataclass
from datetime import date

from ..errors import InvalidInputError
from .breeds import SIZE_CLASSES, SizeClass
from .database import Database

MAX_BREED_LENGTH = 60


@dataclass(frozen=True)
class PetProfile:
    """保存済みのプロフィール。未入力の項目は空（None か空文字）。"""

    birth: date | None
    breed: str
    size_class: SizeClass | None
    adult_grams: int | None
    neutered: bool | None
    food_id: str | None


def _size(value: object) -> SizeClass | None:
    return next((size for size in SIZE_CLASSES if size == value), None)


def load_profile(database: Database) -> PetProfile:
    with database.connect() as db:
        row = db.execute(
            "SELECT birth, breed, size_class, adult_grams, neutered, food_id "
            "FROM pet_profile WHERE id = 1"
        ).fetchone()
    if row is None:
        return PetProfile(None, "", None, None, None, None)
    return PetProfile(
        birth=date.fromisoformat(row["birth"]) if row["birth"] else None,
        breed=row["breed"] or "",
        size_class=_size(row["size_class"]),
        adult_grams=row["adult_grams"],
        neutered=None if row["neutered"] is None else bool(row["neutered"]),
        food_id=row["food_id"] or None,
    )


def save_profile(
    database: Database,
    *,
    breed: str,
    size_class: str,
    adult_grams: int | None,
    neutered: str,
    food_id: str,
) -> None:
    """生年月日以外の項目を保存する。行がなければ作る。"""
    name = breed.strip()
    if len(name) > MAX_BREED_LENGTH or any(unicodedata.category(char) == "Cc" for char in breed):
        raise InvalidInputError("犬種は60文字以内で入力してください")
    if size_class and size_class not in SIZE_CLASSES:
        raise InvalidInputError("体格を選んでください")
    if neutered not in ("1", "0", ""):
        raise InvalidInputError("去勢・避妊の有無を選んでください")
    with database.connect() as db:
        if (
            food_id
            and not db.execute(
                "SELECT 1 FROM foods WHERE id = ? AND active = 1", (food_id,)
            ).fetchone()
        ):
            raise InvalidInputError("選んだフードが見つかりません")
        db.execute(
            "INSERT INTO pet_profile (id, breed, size_class, adult_grams, neutered, food_id) "
            "VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "breed=excluded.breed, size_class=excluded.size_class, "
            "adult_grams=excluded.adult_grams, neutered=excluded.neutered, "
            "food_id=excluded.food_id",
            (
                name or None,
                size_class or None,
                adult_grams,
                int(neutered) if neutered else None,
                food_id or None,
            ),
        )
