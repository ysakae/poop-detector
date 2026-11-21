"""相談の指示文に入れる、犬の状況（目安・体重・給餌・排泄・フード）を集める。"""

import json
from dataclasses import dataclass
from datetime import date, timedelta

from ...records.care import feeding_totals
from ...records.database import Database
from ...records.events import Store
from ...records.feeding_advice import (
    Advice,
    DayIntake,
    compute_advice,
    intake_summary,
    weekly_gain,
)
from ...records.feeding_plans import plans_for_day
from ...records.foods import Food, active_foods, kcal_by_name, profile_kcal
from ...records.profile import PetProfile, load_profile
from ...records.weights import Weight, weights
from ...timezones import to_local

WEIGHT_DAYS = 90
MAX_WEIGHT_POINTS = 30
RECENT_DAYS = 14


@dataclass(frozen=True)
class AdviceContext:
    """指示文の材料。excretions は（日付、排便の数、排尿の数）。"""

    pet_name: str
    profile: PetProfile
    advice: Advice
    weights: tuple[Weight, ...]
    intake: tuple[DayIntake, ...]
    meals: int
    excretions: tuple[tuple[date, int, int], ...]
    foods: tuple[Food, ...]
    # 間引く前の全体重から求めた直近28日の伸び（g/週）。画面と同じ値。
    weekly_gain_g: float | None = None
    # 日ごとのフード名と量（g）。intake と同じ並び。
    intake_foods: tuple[tuple[tuple[str, float], ...], ...] = ()


def _thin(rows: list[Weight]) -> tuple[Weight, ...]:
    """多いときは等間隔に間引く。最初と最新は残す。"""
    if len(rows) <= MAX_WEIGHT_POINTS:
        return tuple(rows)
    last = len(rows) - 1
    steps = MAX_WEIGHT_POINTS - 1
    return tuple(rows[round(i * last / steps)] for i in range(MAX_WEIGHT_POINTS))


def _excretions(store: Store, days: list[date]) -> tuple[tuple[date, int, int], ...]:
    feces = dict.fromkeys(days, 0)
    urine = dict.fromkeys(days, 0)
    for row in store.records():
        day = to_local(row["actual_start"] or row["timestamp"]).date()
        if day not in feces:
            continue
        kind = row["excretion_type"]
        if kind in ("feces", "both"):
            feces[day] += 1
        if kind in ("urine", "both"):
            urine[day] += 1
    return tuple((day, feces[day], urine[day]) for day in days)


def build_context(database: Database, store: Store, pet_name: str, today: date) -> AdviceContext:
    """直近の記録から相談の材料を作る。体重は90日、給餌と排泄は14日分。"""
    profile = load_profile(database)
    rows = weights(database, today - timedelta(days=WEIGHT_DAYS - 1), today)
    meals = len(plans_for_day(database, today))
    advice = compute_advice(
        profile, rows[-1] if rows else None, profile_kcal(database, profile.food_id), meals, today
    )
    days = [today - timedelta(days=offset) for offset in range(RECENT_DAYS - 1, -1, -1)]
    totals = feeding_totals(database, days[0], today)
    return AdviceContext(
        pet_name=pet_name,
        profile=profile,
        advice=advice,
        weights=_thin(rows),
        intake=intake_summary(totals, kcal_by_name(database), days),
        meals=meals,
        excretions=_excretions(store, days),
        foods=tuple(active_foods(database)),
        weekly_gain_g=weekly_gain(rows, today),
        intake_foods=tuple(
            tuple(sorted((name, dg / 10) for name, dg in totals.get(day, {}).items()))
            for day in days
        ),
    )


def basis_text(advice: Advice) -> str:
    """質問を送った時点の目安の要約（短い JSON）。目安が出せないときは空のオブジェクト。"""
    guide = advice.guide
    if guide is None:
        return "{}"
    grams = guide.grams_per_day
    return json.dumps(
        {
            "months": guide.months,
            "weight_grams": guide.weight_grams,
            "kcal_per_day": round(guide.kcal_per_day),
            "grams_per_day": None if grams is None else round(grams),
        },
        ensure_ascii=False,
    )
