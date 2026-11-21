from datetime import date
from pathlib import Path

import pytest

from poop_detector.errors import InvalidInputError
from poop_detector.records.breeds import BREEDS, find_breed, size_from_adult
from poop_detector.records.database import Database
from poop_detector.records.profile import PetProfile, load_profile, save_profile
from poop_detector.records.weights import save_birth_date


def test_size_from_adult_thresholds() -> None:
    cases = {
        3999: "toy",
        4000: "small",
        9999: "small",
        10000: "medium",
        24999: "medium",
        25000: "large",
        44999: "large",
        45000: "giant",
    }
    for grams, expected in cases.items():
        assert size_from_adult(grams) == expected


def test_breed_table_sizes_match_their_weights() -> None:
    assert len({breed.name for breed in BREEDS}) == len(BREEDS)
    for breed in BREEDS:
        assert size_from_adult(breed.adult_grams) == breed.size_class, breed.name


def test_find_breed_matches_exact_name() -> None:
    breed = find_breed(" 柴 ")
    assert breed is not None
    assert breed.size_class == size_from_adult(breed.adult_grams)
    assert find_breed("雑種") is None


def test_empty_profile(tmp_path: Path) -> None:
    assert load_profile(Database(tmp_path / "e.sqlite3")) == PetProfile(
        None, "", None, None, None, None
    )


def test_profile_round_trip_keeps_birth(tmp_path: Path) -> None:
    database = Database(tmp_path / "e.sqlite3")
    save_birth_date(database, "2024-01-02")
    save_profile(
        database, breed="柴", size_class="medium", adult_grams=9000, neutered="1", food_id=""
    )
    profile = load_profile(database)
    assert profile.birth == date(2024, 1, 2)
    assert profile.breed == "柴"
    assert profile.size_class == "medium"
    assert profile.adult_grams == 9000
    assert profile.neutered is True
    assert profile.food_id is None


def test_profile_created_without_row_and_cleared(tmp_path: Path) -> None:
    database = Database(tmp_path / "e.sqlite3")
    save_profile(database, breed="", size_class="", adult_grams=None, neutered="0", food_id="")
    profile = load_profile(database)
    assert profile.neutered is False
    assert profile.size_class is None
    save_profile(database, breed="", size_class="", adult_grams=None, neutered="", food_id="")
    assert load_profile(database).neutered is None


def test_profile_accepts_active_food_only(tmp_path: Path) -> None:
    database = Database(tmp_path / "e.sqlite3")
    with database.connect() as db:
        db.execute(
            "INSERT INTO foods (id, name, kcal_dk, created_at, active) VALUES "
            "('a', 'A', 3500, 'x', 1), ('b', 'B', 3500, 'x', 0)"
        )
    save_profile(database, breed="", size_class="", adult_grams=None, neutered="", food_id="a")
    assert load_profile(database).food_id == "a"
    with pytest.raises(InvalidInputError, match="選んだフードが見つかりません"):
        save_profile(database, breed="", size_class="", adult_grams=None, neutered="", food_id="b")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"size_class": "huge"}, "体格を選んでください"),
        ({"food_id": "nope"}, "選んだフードが見つかりません"),
        ({"neutered": "2"}, "去勢・避妊の有無を選んでください"),
        ({"breed": "あ" * 61}, "犬種は60文字以内で入力してください"),
        ({"breed": "柴\n"}, "犬種は60文字以内で入力してください"),
    ],
)
def test_profile_rejects_invalid_input(
    tmp_path: Path, kwargs: dict[str, str], message: str
) -> None:
    values = {"breed": "", "size_class": "", "neutered": "", "food_id": ""} | kwargs
    with pytest.raises(InvalidInputError, match=message):
        save_profile(Database(tmp_path / "e.sqlite3"), adult_grams=None, **values)


def test_profile_accepts_full_width_space_in_breed(tmp_path: Path) -> None:
    database = Database(tmp_path / "e.sqlite3")
    save_profile(
        database, breed="トイ　プードル", size_class="", adult_grams=None, neutered="", food_id=""
    )
    assert load_profile(database).breed == "トイ　プードル"
