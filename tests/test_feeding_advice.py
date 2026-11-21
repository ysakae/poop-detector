from datetime import date
from pathlib import Path

import pytest

from poop_detector.records.breeds import find_breed
from poop_detector.records.care import CareEntry, delete_entry, feeding_totals, save_entry
from poop_detector.records.database import Database
from poop_detector.records.feeding_advice import (
    compute_advice,
    intake_summary,
    weekly_gain,
)
from poop_detector.records.profile import PetProfile
from poop_detector.records.weights import Weight
from poop_detector.timezones import parse_local

TODAY = date(2026, 11, 21)
STALE = "体重の記録が{}日前のものです。量り直すと目安が正確になります。"
SIZE_WARNING = "犬種か成犬時の予想体重を入れると目安が正確になります。"
KCAL_WARNING = "フードのカロリーが分からないので、量（g）は出せません。"


def profile(**overrides: object) -> PetProfile:
    base: dict[str, object] = {
        "birth": date(2026, 6, 15),
        "breed": "",
        "size_class": "small",
        "adult_grams": 5000,
        "neutered": None,
        "food_id": None,
    }
    return PetProfile(**{**base, **overrides})  # type: ignore[arg-type]


def weight(grams: int, day: date = date(2026, 11, 18)) -> Weight:
    return Weight("w", day, grams, "")


def test_puppy_guide_matches_hand_calculation() -> None:
    advice = compute_advice(profile(), weight(3200), 3850, 3, TODAY)
    guide = advice.guide
    assert guide is not None
    assert guide.months == 5
    assert guide.stage == "growth"
    assert guide.factor == 2.5
    assert guide.size_source == "profile"
    assert guide.rer == pytest.approx(167.48, abs=0.01)
    assert guide.kcal_per_day == pytest.approx(418.70, abs=0.01)
    assert guide.grams_per_day == pytest.approx(108.75, abs=0.01)
    assert guide.grams_per_meal == pytest.approx(36.25, abs=0.01)
    assert advice.warnings == ()
    assert advice.missing == ()


def test_months_are_full_calendar_months() -> None:
    p = profile()
    before = compute_advice(p, weight(3200, date(2026, 11, 14)), 3850, 3, date(2026, 11, 14))
    on = compute_advice(p, weight(3200, date(2026, 11, 15)), 3850, 3, date(2026, 11, 15))
    assert before.guide is not None
    assert before.guide.months == 4
    assert on.guide is not None
    assert on.guide.months == 5


@pytest.mark.parametrize(("ratio", "factor"), [(0.49, 3.0), (0.5, 2.5), (0.79, 2.5), (0.8, 2.0)])
def test_ratio_thresholds(ratio: float, factor: float) -> None:
    advice = compute_advice(
        profile(adult_grams=10000), weight(round(ratio * 10000)), 3850, 3, TODAY
    )
    assert advice.guide is not None
    assert advice.guide.ratio == pytest.approx(ratio)
    assert advice.guide.factor == factor


@pytest.mark.parametrize(
    ("neutered", "kcal", "factor"),
    [(True, 629.81, 1.6), (False, 708.54, 1.8), (None, 629.81, 1.6)],
)
def test_adult_factor_by_neutering(neutered: bool | None, kcal: float, factor: float) -> None:
    p = profile(birth=date(2024, 11, 21), size_class="medium", adult_grams=10000, neutered=neutered)
    advice = compute_advice(p, weight(10000), 3500, 2, TODAY)
    guide = advice.guide
    assert guide is not None
    assert (guide.months, guide.stage, guide.factor) == (24, "adult", factor)
    assert guide.kcal_per_day == pytest.approx(kcal, abs=0.02)


def test_unknown_size_defaults_to_medium_with_warning() -> None:
    p = profile(birth=date(2025, 12, 21), size_class=None, adult_grams=None)
    advice = compute_advice(p, weight(8000), 3500, 2, TODAY)
    guide = advice.guide
    assert guide is not None
    assert (guide.months, guide.stage, guide.factor) == (11, "growth", 2.0)
    assert guide.size_source == "default"
    assert guide.size_class == "medium"
    assert guide.adult_grams is None
    assert guide.ratio is None
    assert advice.warnings == (SIZE_WARNING,)


def test_growth_without_expected_weight_uses_age_factor() -> None:
    p = profile(size_class="small", adult_grams=None)
    young = compute_advice(p, weight(2000), 3500, 2, TODAY)
    assert young.guide is not None
    assert young.guide.factor == 2.0
    younger = compute_advice(p, weight(1500, date(2026, 9, 1)), 3500, 2, date(2026, 9, 1))  # 2か月
    assert younger.guide is not None
    assert younger.guide.factor == 3.0
    assert younger.warnings == (SIZE_WARNING,)


def test_breed_table_supplies_adult_weight() -> None:
    p = profile(breed="柴", size_class=None, adult_grams=None)
    advice = compute_advice(p, weight(4000), 3500, 2, TODAY)
    breed = find_breed("柴")
    assert breed is not None
    assert advice.guide is not None
    assert advice.guide.size_source == "estimated"
    assert advice.guide.adult_grams == breed.adult_grams
    assert advice.guide.size_class == breed.size_class
    assert SIZE_WARNING not in advice.warnings


def test_stale_weight_and_missing_kcal_warn_but_still_compute() -> None:
    advice = compute_advice(profile(), weight(3200, date(2026, 11, 6)), None, 3, TODAY)
    assert advice.guide is not None
    assert advice.guide.grams_per_day is None
    assert advice.guide.grams_per_meal is None
    assert advice.guide.kcal_per_day > 0
    assert advice.warnings == (STALE.format(15), KCAL_WARNING)
    fresh = compute_advice(profile(), weight(3200, date(2026, 11, 7)), 3850, 3, TODAY)
    assert fresh.warnings == ()


def test_adult_stale_threshold_is_sixty_days() -> None:
    p = profile(birth=date(2024, 11, 21), size_class="medium", adult_grams=10000)
    ok = compute_advice(p, weight(10000, date(2026, 9, 22)), 3500, 2, TODAY)
    old = compute_advice(p, weight(10000, date(2026, 9, 21)), 3500, 2, TODAY)
    assert ok.warnings == ()
    assert old.warnings == (STALE.format(61),)


def test_zero_meals_gives_no_per_meal_amount() -> None:
    advice = compute_advice(profile(), weight(3200), 3850, 0, TODAY)
    assert advice.guide is not None
    assert advice.guide.grams_per_day is not None
    assert advice.guide.grams_per_meal is None


def test_under_two_months_warns() -> None:
    p = profile(birth=date(2026, 10, 1))
    advice = compute_advice(p, weight(1200), 3850, 3, TODAY)
    assert advice.guide is not None
    assert advice.guide.months == 1
    assert (
        "生後2か月未満の目安はこの計算の対象外です。獣医師に相談してください。" in advice.warnings
    )


def test_missing_birth_or_weight_gives_no_guide() -> None:
    no_birth = compute_advice(profile(birth=None), weight(3200), 3850, 3, TODAY)
    assert (no_birth.guide, no_birth.missing) == (None, ("生年月日を入れてください",))
    no_weight = compute_advice(profile(), None, 3850, 3, TODAY)
    assert (no_weight.guide, no_weight.missing) == (None, ("体重を記録してください",))
    neither = compute_advice(profile(birth=None), None, 3850, 3, TODAY)
    assert neither.missing == ("生年月日を入れてください", "体重を記録してください")


def test_birth_after_weight_day_is_rejected() -> None:
    advice = compute_advice(profile(birth=date(2026, 11, 19)), weight(3200), 3850, 3, TODAY)
    assert advice.guide is None
    assert advice.missing == ("生年月日を確かめてください",)


def test_weekly_gain_uses_last_28_days() -> None:
    rows = [
        weight(1000, date(2026, 10, 1)),
        weight(3000, date(2026, 11, 7)),
        weight(3120, date(2026, 11, 14)),
    ]
    assert weekly_gain(rows, TODAY) == pytest.approx(120.0)
    assert weekly_gain(rows[:2], TODAY) is None
    assert weekly_gain([rows[2]], TODAY) is None
    assert weekly_gain([], TODAY) is None
    same_day = [weight(3000, date(2026, 11, 7)), weight(3100, date(2026, 11, 7))]
    assert weekly_gain(same_day, TODAY) is None


def test_intake_summary_kcal_only_when_all_foods_known() -> None:
    days = [date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 21)]
    totals = {
        days[0]: {"A": 600, "B": 400},
        days[1]: {"A": 500, "謎": 100},
    }
    summary = intake_summary(totals, {"A": 4000, "B": 3500}, days)
    assert [s.day for s in summary] == days
    assert summary[0].grams == pytest.approx(100.0)
    assert summary[0].kcal == pytest.approx(60 * 4.0 + 40 * 3.5)
    assert summary[1].grams == pytest.approx(60.0)
    assert summary[1].kcal is None
    assert (summary[2].grams, summary[2].kcal) == (0.0, 0.0)


def test_feeding_totals_excludes_deleted_and_amountless(tmp_path: Path) -> None:
    database = Database(tmp_path / "e.sqlite3")

    def add(at: str, kind: str, food: str, amount: int | None) -> str:
        return save_entry(database, CareEntry("", parse_local(at), kind, "家族", "", food, amount))

    add("2024-03-01T00:00", "feeding", "A", 200)
    add("2024-03-01T23:59", "feeding", " A ", 300)
    add("2024-03-01T12:00", "feeding", "B", 100)
    add("2024-03-02T00:00", "feeding", "A", 50)
    add("2024-03-01T09:00", "feeding", "A", None)
    add("2024-03-01T09:00", "sheet", "", None)
    gone = add("2024-03-01T10:00", "feeding", "A", 999)
    delete_entry(database, gone)
    add("2024-02-29T23:59", "feeding", "A", 1)
    add("2024-03-04T00:00", "feeding", "A", 2)
    totals = feeding_totals(database, date(2024, 3, 1), date(2024, 3, 3))
    assert totals == {date(2024, 3, 1): {"A": 500, "B": 100}, date(2024, 3, 2): {"A": 50}}


def test_entered_adult_weight_suppresses_adult_weight_warning() -> None:
    p = profile(size_class=None, adult_grams=5000)
    advice = compute_advice(p, weight(3200), 3850, 3, TODAY)
    assert advice.guide is not None
    assert advice.guide.size_source == "estimated"
    assert SIZE_WARNING not in advice.warnings


def test_future_birth_or_weight_day_is_rejected() -> None:
    future_birth = compute_advice(profile(birth=date(2026, 11, 22)), weight(3200), 3850, 3, TODAY)
    assert future_birth.guide is None
    assert future_birth.missing == ("生年月日を確かめてください",)
    future_weight = compute_advice(profile(), weight(3200, date(2026, 11, 22)), 3850, 3, TODAY)
    assert future_weight.guide is None
    assert future_weight.missing == ("体重の測定日を確かめてください",)
