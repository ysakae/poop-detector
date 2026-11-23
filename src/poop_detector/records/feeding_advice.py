"""月齢・体重・体格から、1日のごはんの量の目安を計算する。"""

from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from .breeds import ADULT_MONTHS, SizeClass, find_breed, size_from_adult
from .profile import PetProfile
from .weights import Weight

STALE_GROWTH_DAYS = 14
STALE_ADULT_DAYS = 60
GAIN_WINDOW_DAYS = 28
FEDIAF_MIN_AGE_DAYS = 56
FEDIAF_MAX_AGE_MONTHS = 12
ADULT_TRANSITION_DAYS = 28
EARLY_MONTHS = 4
DEFAULT_SIZE: SizeClass = "medium"


@dataclass(frozen=True)
class Guide:
    """計算の結果。成長期か成犬か、使った係数、1日と1回の量。"""

    months: int
    weight_grams: int
    weight_day: date
    size_class: SizeClass
    size_source: Literal["profile", "estimated", "default"]
    adult_grams: int | None
    ratio: float | None
    stage: Literal["growth", "adult"]
    rer: float
    factor: float
    kcal_per_day: float
    grams_per_day: float | None
    grams_per_meal: float | None
    meals: int
    fediaf_adult_share: float | None = None

    @property
    def formula_label(self) -> str:
        if self.fediaf_adult_share is None:
            return "成犬用の式" if self.stage == "adult" else "従来の成長期の式"
        if self.fediaf_adult_share == 0:
            return "FEDIAFの成長期の式"
        return f"FEDIAFの成長期の式から移行中（成犬用 {self.fediaf_adult_share:.0%}）"


@dataclass(frozen=True)
class Advice:
    """目安（足りない入力があれば None）と、注意・不足の文言。"""

    guide: Guide | None
    warnings: tuple[str, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class DayIntake:
    """1日に記録したごはんの量（g）と、分かる場合のカロリー。"""

    day: date
    grams: float
    kcal: float | None


def _month_anniversary(birth: date, months: int) -> date:
    years, month_index = divmod(birth.month - 1 + months, 12)
    year, month = birth.year + years, month_index + 1
    return date(year, month, min(birth.day, monthrange(year, month)[1]))


def _full_months(birth: date, today: date) -> int:
    months = (today.year - birth.year) * 12 + today.month - birth.month
    return months - (1 if today < _month_anniversary(birth, months) else 0)


def _size(
    profile: PetProfile,
) -> tuple[SizeClass, Literal["profile", "estimated", "default"], int | None]:
    breed = find_breed(profile.breed)
    adult = profile.adult_grams or (breed.adult_grams if breed else None)
    if profile.size_class is not None:
        return profile.size_class, "profile", adult
    if adult is not None:
        return size_from_adult(adult), "estimated", adult
    return DEFAULT_SIZE, "default", None


def _growth_factor(months: int, ratio: float | None) -> float:
    if ratio is None:
        return 3.0 if months < EARLY_MONTHS else 2.0
    if ratio < 0.5:
        return 3.0
    return 2.5 if ratio < 0.8 else 2.0


def _missing_inputs(birth: date | None, latest: Weight | None, today: date) -> tuple[str, ...]:
    missing = []
    if birth is None:
        missing.append("生年月日を入れてください")
    if latest is None:
        missing.append("体重を記録してください")
    if birth is not None and (birth > today or (latest is not None and birth > latest.day)):
        missing.append("生年月日を確かめてください")
    if latest is not None and latest.day > today:
        missing.append("体重の測定日を確かめてください")
    return tuple(missing)


def _guide_warnings(guide: Guide, today: date, age_days: int) -> tuple[str, ...]:
    warnings = []
    age = (today - guide.weight_day).days
    if age > (STALE_GROWTH_DAYS if guide.stage == "growth" else STALE_ADULT_DAYS):
        warnings.append(f"体重の記録が{age}日前のものです。量り直すと目安が正確になります。")
    if guide.adult_grams is None:
        warnings.append("犬種か成犬時の予想体重を入れると目安が正確になります。")
    if guide.grams_per_day is None:
        warnings.append("フードのカロリーが分からないので、量（g）は出せません。")
    if age_days < FEDIAF_MIN_AGE_DAYS:
        warnings.append("生後8週未満の目安はこの計算の対象外です。獣医師に相談してください。")
    if guide.fediaf_adult_share is not None and guide.ratio is not None and guide.ratio > 1:
        warnings.append("現在の体重が成犬時の予想体重を超えています。予想体重を確かめてください。")
    return tuple(warnings)


def compute_advice(
    profile: PetProfile, latest: Weight | None, kcal_dk: int | None, meals: int, today: date
) -> Advice:
    """プロフィールと最新の体重から目安を求める。kcal_dk は 100 g あたり 0.1 kcal 単位。"""
    missing = _missing_inputs(profile.birth, latest, today)
    if missing or profile.birth is None or latest is None:
        return Advice(None, (), missing)

    months = _full_months(profile.birth, today)
    age_days = (today - profile.birth).days
    size, source, adult = _size(profile)
    ratio = latest.grams / adult if adult else None
    adult_months = ADULT_MONTHS[size]
    adult_factor = 1.8 if profile.neutered is False else 1.6
    stage: Literal["growth", "adult"] = "growth" if months < adult_months else "adult"
    adult_share = None
    if stage == "adult":
        factor = adult_factor
    elif (
        ratio is not None
        and age_days >= FEDIAF_MIN_AGE_DAYS
        and adult_months <= FEDIAF_MAX_AGE_MONTHS
    ):
        adult_day = _month_anniversary(profile.birth, adult_months)
        days_to_adult = (adult_day - today).days
        adult_share = max(0.0, 1 - days_to_adult / ADULT_TRANSITION_DAYS)
        # 成犬時の予想体重を超えている場合は、成長期の係数を成犬体重相当で止める。
        growth_factor = (254.1 - 135.0 * min(ratio, 1.0)) / 70
        factor = growth_factor * (1 - adult_share) + adult_factor * adult_share
    else:
        factor = _growth_factor(months, ratio)
    rer = 70 * (latest.grams / 1000) ** 0.75
    kcal = rer * factor
    per_day = kcal / (kcal_dk / 1000) if kcal_dk is not None and kcal_dk > 0 else None
    per_meal = per_day / meals if per_day is not None and meals >= 1 else None

    guide = Guide(
        months=months,
        weight_grams=latest.grams,
        weight_day=latest.day,
        size_class=size,
        size_source=source,
        adult_grams=adult,
        ratio=ratio,
        stage=stage,
        rer=rer,
        factor=factor,
        kcal_per_day=kcal,
        grams_per_day=per_day,
        grams_per_meal=per_meal,
        meals=meals,
        fediaf_adult_share=adult_share,
    )
    return Advice(guide, _guide_warnings(guide, today, age_days), ())


def weekly_gain(rows: Sequence[Weight], today: date) -> float | None:
    """直近28日の測定から、最小二乗の傾きを1週間あたりのグラムで返す。"""
    since = today - timedelta(days=GAIN_WINDOW_DAYS)
    points = [(row.day.toordinal(), row.grams) for row in rows if row.day >= since]
    if len({x for x, _ in points}) < 2:
        return None
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    spread = sum((x - mean_x) ** 2 for x, _ in points)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / spread
    return slope * 7


def intake_summary(
    totals: Mapping[date, Mapping[str, int]],
    kcal_by_food: Mapping[str, int],
    days: Sequence[date],
) -> tuple[DayIntake, ...]:
    """日ごとの給餌量（g）と、すべてのフードが台帳にある日のカロリー。記録のない日は 0 g・0 kcal。"""
    summary = []
    for day in days:
        foods = totals.get(day, {})
        grams = sum(foods.values()) / 10
        kcal = None
        if all(food in kcal_by_food for food in foods):
            kcal = sum(dg / 10 * kcal_by_food[food] / 1000 for food, dg in foods.items())
        summary.append(DayIntake(day, grams, kcal))
    return tuple(summary)
