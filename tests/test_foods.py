"""フードの台帳とカロリー探しの依頼を検証する。"""

import pytest

from poop_detector.errors import InvalidInputError
from poop_detector.records import foods
from poop_detector.records.care import CareEntry, save_entry
from poop_detector.records.database import Database
from poop_detector.timezones import parse_local


@pytest.fixture
def database(tmp_path):
    return Database(tmp_path / "e.sqlite3")


def add(database, name="ふつう", kcal_dk=3850, **extra):
    return foods.save_food(
        database, name=name, kcal_dk=kcal_dk, source_url="", user_id="u", **extra
    )


def test_parse_kcal_range_and_step():
    assert foods.parse_kcal("385") == 3850
    assert foods.parse_kcal("385.5") == 3855
    assert foods.parse_kcal("385,5") == 3855
    for bad in ("29.9", "700.1", "385.55", "abc", "３８５", ""):
        with pytest.raises(InvalidInputError, match="30〜700 kcal/100g"):
            foods.parse_kcal(bad)


def test_parse_source_url_requires_https():
    assert foods.parse_source_url("") == ""
    assert foods.parse_source_url("https://example.com/a") == "https://example.com/a"
    for bad in (
        "http://x",
        "javascript:alert(1)",
        "https://a b",
        "https://a\n",
        "https://" + "a" * 500,
    ):
        with pytest.raises(InvalidInputError, match="httpsのURL"):
            foods.parse_source_url(bad)


def test_save_food_rejects_duplicate_name(database):
    first = add(database, " ふつう ")
    with pytest.raises(InvalidInputError, match="同じ名前のフード"):
        add(database, "ふつう")
    other = add(database, "べつ")
    with pytest.raises(InvalidInputError, match="同じ名前のフード"):
        add(database, "ふつう", food_id=other)
    assert foods.kcal_by_name(database) == {"ふつう": 3850, "べつ": 3850}
    add(database, "ふつう", 3900, food_id=first)
    assert foods.kcal_by_name(database)["ふつう"] == 3900


def test_save_food_validates_name_and_missing_id(database):
    with pytest.raises(InvalidInputError):
        add(database, "  ")
    with pytest.raises(InvalidInputError):
        add(database, "あ" * 81)
    with pytest.raises(InvalidInputError):
        add(database, food_id="missing")


def test_stopped_food_leaves_active_list(database):
    keep = add(database, "b")
    gone = add(database, "a")
    assert [f.name for f in foods.active_foods(database)] == ["a", "b"]
    foods.stop_food(database, gone)
    assert [f.id for f in foods.active_foods(database)] == [keep]
    assert foods.kcal_by_name(database) == {"b": 3850}
    with pytest.raises(InvalidInputError):
        foods.stop_food(database, "missing")


def test_profile_kcal_ignores_stopped_and_empty(database):
    food = add(database)
    assert foods.profile_kcal(database, food) == 3850
    assert foods.profile_kcal(database, None) is None
    assert foods.profile_kcal(database, "") is None
    assert foods.profile_kcal(database, "missing") is None
    foods.stop_food(database, food)
    assert foods.profile_kcal(database, food) is None


def test_request_lookup_reuses_pending_request_for_same_name(database):
    first = foods.request_lookup(database, "ドッグフード", "u")
    assert foods.request_lookup(database, "ドッグフード", "v") == first
    with database.connect() as db:
        db.execute("UPDATE food_lookups SET status='running' WHERE id=?", (first,))
    assert foods.request_lookup(database, "ドッグフード", "u") == first
    with database.connect() as db:
        db.execute("UPDATE food_lookups SET status='done' WHERE id=?", (first,))
    assert foods.request_lookup(database, "ドッグフード", "u") != first
    assert foods.request_lookup(database, "べつ", "u") not in (first,)


def test_recent_lookups_newest_first_with_limit(database):
    old = foods.request_lookup(database, "a", "u", now=parse_local("2026-11-20T09:00"))
    new = foods.request_lookup(database, "b", "u", now=parse_local("2026-11-21T09:00"))
    rows = foods.recent_lookups(database)
    assert [r.id for r in rows] == [new, old]
    assert rows[0].status == "queued"
    assert rows[0].kcal_dk is None
    assert rows[0].failure is None
    assert [r.id for r in foods.recent_lookups(database, limit=1)] == [new]


def test_unregistered_names_lists_feeding_foods_not_in_ledger(database):
    at = parse_local("2026-11-21T08:00")
    for food in ("古い", "ふつう", "新しい"):
        at = at.replace(hour=at.hour + 1)
        save_entry(database, CareEntry("", at, "feeding", "u", "", food, 100))
    add(database, "ふつう")
    assert foods.unregistered_names(database) == ["新しい", "古い"]
