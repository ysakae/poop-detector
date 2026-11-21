"""ごはんの目安と相談の画面に出す値を整える。"""

from dataclasses import dataclass
from datetime import date, timedelta

from ..features.advice.context import current_advice
from ..features.advice.jobs import waiting_position
from ..records.advice import Message, Thread
from ..records.breeds import SIZE_NAMES
from ..records.care import feeding_totals
from ..records.database import Database
from ..records.feeding_advice import Advice, Guide, intake_summary, weekly_gain
from ..records.foods import kcal_by_name
from ..records.profile import PetProfile, load_profile
from ..records.weights import Weight, weights
from ..timezones import to_local

INTAKE_DAYS = 7
DISCLAIMER = "目安です。獣医師の指示があればそちらを優先してください。"
STATE_NAMES = {"queued": "回答待ち", "running": "回答待ち", "done": "回答済み", "failed": "失敗"}
ANSWER_FAILURES = {
    "timeout": "時間切れでした",
    "rate_limited": "混み合っていました",
}
DEFAULT_FAILURE = "回答を作れませんでした"
WORKING = "回答を作っています"


@dataclass(frozen=True)
class Snapshot:
    """画面を作る時点の、プロフィール・体重・目安。"""

    profile: PetProfile
    weights: list[Weight]
    advice: Advice


def snapshot(database: Database, today: date) -> Snapshot:
    """記録から目安を計算する。止めたフードのカロリーは使わない。"""
    profile = load_profile(database)
    rows = weights(database)
    return Snapshot(profile, rows, current_advice(database, today, rows))


def today_grams(database: Database, today: date) -> tuple[int, bool] | None:
    """今日の1日の量の目安（g、1 g 単位）と、警告があるか。出せないときは None。"""
    advice = snapshot(database, today).advice
    guide = advice.guide
    if guide is None or guide.grams_per_day is None:
        return None
    return round(guide.grams_per_day), bool(advice.warnings)


def _headline(guide: Guide) -> str:
    parts = [
        f"{guide.months}か月・{guide.weight_grams / 1000:g} kg",
        f"{guide.weight_day.month}/{guide.weight_day.day} 測定",
        SIZE_NAMES[guide.size_class],
    ]
    if guide.adult_grams is not None:
        parts.append(f"成犬 約{guide.adult_grams / 1000:g} kg予想")
    return f"{parts[0]}（{'・'.join(parts[1:])}）"


def _meal_text(guide: Guide) -> str:
    if guide.grams_per_meal is None:
        return ""
    return f"（1回 {round(guide.grams_per_meal)} g × {guide.meals}回）"


def guide_view(guide: Guide | None, food_text: str) -> dict[str, object] | None:
    """目安の表示用の値。目安が出せないときは None。"""
    if guide is None:
        return None
    grams = guide.grams_per_day
    return {
        "headline": _headline(guide),
        "kcal": round(guide.kcal_per_day),
        "grams": None if grams is None else round(grams),
        "meal": _meal_text(guide),
        "food": food_text,
        "formula": (
            f"安静時 {guide.rer:.0f} kcal × 係数 {guide.factor:g}"
            + (f"（成犬の予想体重の {guide.ratio:.0%}）" if guide.ratio is not None else "")
        ),
    }


def intake_view(database: Database, today: date) -> tuple[list[dict[str, str]], str]:
    """直近7日の給餌の行と、記録のある日の平均の文。"""
    days = [today - timedelta(days=offset) for offset in range(INTAKE_DAYS - 1, -1, -1)]
    totals = feeding_totals(database, days[0], today)
    summary = intake_summary(totals, kcal_by_name(database), days)
    rows = [
        {
            "day": f"{item.day.month}/{item.day.day}",
            "grams": f"{item.grams:g} g",
            "kcal": "" if item.kcal is None else f"{item.kcal:.0f} kcal",
        }
        for item in summary
    ]
    recorded = [item.grams for item in summary if item.grams > 0]
    average = f"記録のある日の平均 {sum(recorded) / len(recorded):.0f} g/日" if recorded else ""
    return rows, average


def gain_text(rows: list[Weight], today: date) -> str:
    gain = weekly_gain(rows, today)
    if gain is None:
        return ""
    grams = round(gain)
    return f"{grams:+d} g/週" if grams else "±0 g/週"


def thread_rows(threads: list[Thread]) -> list[dict[str, str]]:
    return [
        {
            "id": item.id,
            "title": item.title,
            "updated": to_local(item.updated_at.isoformat()).strftime("%m/%d"),
            "author": item.author_name,
            "state": STATE_NAMES.get(item.state, ""),
        }
        for item in threads
    ]


def failure_text(kind: str | None) -> str:
    return ANSWER_FAILURES.get(kind or "", DEFAULT_FAILURE)


def message_rows(database: Database, items: list[Message]) -> list[dict[str, object]]:
    """会話の画面の1件ずつ。回答待ちの文と失敗の文もここで決める。"""
    rows: list[dict[str, object]] = []
    for item in items:
        waiting = item.role == "assistant" and item.status in ("queued", "running")
        status_text = ""
        if waiting:
            ahead = waiting_position(database, item.id) if item.status == "queued" else 0
            status_text = (
                f"混み合っているので順番を待っています（前に{ahead}件）" if ahead else WORKING
            )
        rows.append(
            {
                "id": item.id,
                "name": "AI" if item.role == "assistant" else item.author_name,
                "assistant": item.role == "assistant",
                "time": to_local(item.created_at.isoformat()).strftime("%m/%d %H:%M"),
                "body": item.body,
                "waiting": waiting,
                "status_text": status_text,
                "failed": item.status == "failed",
                "failure": failure_text(item.failure) if item.status == "failed" else "",
            }
        )
    return rows
