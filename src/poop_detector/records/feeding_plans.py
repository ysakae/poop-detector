"""毎日の給餌の予定と、日ごとの通知済みの印を管理する。"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

from ..errors import InvalidInputError
from ..timezones import local_now
from .database import Database

# 起動直後などで数分遅れたときは知らせる。古い予定は画面で確認できる。
REMINDER_GRACE = timedelta(minutes=10)


@dataclass(frozen=True)
class FeedingPlan:
    """毎日の時刻と、指定日の給餌記録の有無。"""

    id: str
    clock: str
    recorded: bool


def save_plan(
    database: Database, clock: str, *, plan_id: str = "", now: datetime | None = None
) -> str:
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock):
        raise InvalidInputError("ごはんの予定の時刻を入力してください")
    stamp = (now or local_now()).astimezone(UTC).isoformat()
    try:
        with database.connect() as db:
            if plan_id:
                changed = db.execute(
                    "UPDATE feeding_plans SET clock=?, changed_at=? WHERE id=? AND active=1",
                    (clock, stamp, plan_id),
                ).rowcount
                if not changed:
                    raise InvalidInputError("変更する予定が見つかりません")
                return plan_id
            plan_id = uuid4().hex
            db.execute(
                "INSERT INTO feeding_plans (id, clock, changed_at) VALUES (?, ?, ?)",
                (plan_id, clock, stamp),
            )
    except sqlite3.IntegrityError:
        raise InvalidInputError("同じ時刻の予定がすでにあります") from None
    return plan_id


def stop_plan(database: Database, plan_id: str) -> None:
    with database.connect() as db:
        changed = db.execute("UPDATE feeding_plans SET active=0 WHERE id=?", (plan_id,)).rowcount
        if not changed:
            raise InvalidInputError("停止する予定が見つかりません")


def plans_for_day(database: Database, day: date) -> list[FeedingPlan]:
    with database.connect() as db:
        rows = db.execute(
            """SELECT p.id, p.clock, e.id AS entry_id
            FROM feeding_plans p LEFT JOIN care_entries e ON e.plan_id=p.id AND e.plan_day=?
            WHERE p.active=1 ORDER BY p.clock""",
            (day.isoformat(),),
        )
        return [FeedingPlan(row["id"], row["clock"], row["entry_id"] is not None) for row in rows]


def claim_due(database: Database, now: datetime) -> list[str]:
    """未記録で通知前の時刻を確保する。複数の GUI や再起動でも同じ日に再送しない。"""
    now = now.astimezone()
    day = now.date()
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            """SELECT p.id, p.clock, p.changed_at FROM feeding_plans p
            LEFT JOIN care_entries e ON e.plan_id=p.id AND e.plan_day=?
            LEFT JOIN feeding_reminders r ON r.plan_id=p.id AND r.day=?
            WHERE p.active=1 AND e.id IS NULL AND r.plan_id IS NULL ORDER BY p.clock""",
            (day.isoformat(), day.isoformat()),
        ).fetchall()
        due = []
        for row in rows:
            planned = datetime.combine(day, time.fromisoformat(row["clock"])).astimezone()
            change = datetime.fromisoformat(row["changed_at"])
            if not change <= planned <= now or now - planned > REMINDER_GRACE:
                continue
            db.execute(
                "INSERT INTO feeding_reminders (plan_id, day) VALUES (?, ?)",
                (row["id"], day.isoformat()),
            )
            due.append(row["clock"])
    return due
