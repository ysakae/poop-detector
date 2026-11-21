"""相談の回答とカロリー探しの仕事を1件ずつ取り出し、結果を書き戻す。

取り出しは BEGIN IMMEDIATE の中で行うので、複数の処理が同時に呼んでも同じ仕事は渡らない。
時刻は UTC の ISO 8601 で、比較は julianday() で行う（オフセットの違いを吸収するため）。
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from ...records.database import Database

HOURLY_LIMIT = 20
# カロリー探しの時間切れ（300秒）の2倍。これを過ぎた running は取り残しとみなす。
STALE_SECONDS = 600
TIMEOUT_FAILURE = "timeout"


@dataclass(frozen=True)
class Job:
    """取り出した仕事。answer は advice_messages の行、lookup は food_lookups の行。"""

    kind: Literal["answer", "lookup"]
    id: str


@dataclass(frozen=True)
class LookupResult:
    """カロリー探しの結果。kcal_dk が None なら見つからなかった。"""

    product_name: str
    maker: str
    kcal_dk: int | None
    source_url: str
    note: str


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _oldest_answer(db: sqlite3.Connection) -> sqlite3.Row | None:
    row: sqlite3.Row | None = db.execute(
        """SELECT m.id, julianday(m.created_at) AS at FROM advice_messages m
        JOIN advice_threads t ON t.id = m.thread_id
        WHERE m.role='assistant' AND m.status='queued' AND t.deleted_at IS NULL
        ORDER BY julianday(m.created_at), m.id LIMIT 1"""
    ).fetchone()
    return row


def _oldest_lookup(db: sqlite3.Connection) -> sqlite3.Row | None:
    row: sqlite3.Row | None = db.execute(
        """SELECT id, julianday(requested_at) AS at FROM food_lookups
        WHERE status='queued' ORDER BY julianday(requested_at), id LIMIT 1"""
    ).fetchone()
    return row


def claim_next(database: Database, now: datetime, used_last_hour: int) -> Job | None:
    """最も古い待ちの仕事を running にして返す。上限に達しているか、仕事がなければ None。

    削除された相談の回答は取り出さず、待ちのまま残す。
    """
    if used_last_hour >= HOURLY_LIMIT:
        return None
    stamp = _stamp(now)
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        answer = _oldest_answer(db)
        found = _oldest_lookup(db)
        if answer is not None and (found is None or answer["at"] <= found["at"]):
            db.execute(
                "UPDATE advice_messages SET status='running', started_at=? WHERE id=?",
                (stamp, answer["id"]),
            )
            return Job("answer", str(answer["id"]))
        if found is not None:
            db.execute(
                "UPDATE food_lookups SET status='running', started_at=? WHERE id=?",
                (stamp, found["id"]),
            )
            return Job("lookup", str(found["id"]))
        return None


def expire_stale(database: Database, now: datetime) -> int:
    """running のまま STALE_SECONDS を過ぎた行を failed（timeout）にし、件数を返す。"""
    cutoff = _stamp(now - timedelta(seconds=STALE_SECONDS))
    stamp = _stamp(now)
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        answers = db.execute(
            """UPDATE advice_messages SET status='failed', failure=?, answered_at=?
            WHERE status='running' AND julianday(started_at) < julianday(?)""",
            (TIMEOUT_FAILURE, stamp, cutoff),
        ).rowcount
        lookups = db.execute(
            """UPDATE food_lookups SET status='failed', failure=?, finished_at=?
            WHERE status='running' AND julianday(started_at) < julianday(?)""",
            (TIMEOUT_FAILURE, stamp, cutoff),
        ).rowcount
    return int(answers + lookups)


def finish_answer(
    database: Database,
    message_id: str,
    body: str,
    provider: str,
    model: str | None,
    now: datetime,
) -> None:
    """回答を書き込んで done にする。running の行だけを更新する。"""
    with database.connect() as db:
        db.execute(
            """UPDATE advice_messages
            SET body=?, provider=?, model=?, answered_at=?, status='done'
            WHERE id=? AND status='running'""",
            (body, provider, model, _stamp(now), message_id),
        )


def finish_lookup(database: Database, lookup_id: str, result: LookupResult, now: datetime) -> None:
    """探した結果を行に書き込んで done にする。フードの台帳には登録しない。"""
    with database.connect() as db:
        db.execute(
            """UPDATE food_lookups
            SET product_name=?, maker=?, kcal_dk=?, source_url=?, note=?,
                finished_at=?, status='done'
            WHERE id=? AND status='running'""",
            (
                result.product_name,
                result.maker,
                result.kcal_dk,
                result.source_url,
                result.note,
                _stamp(now),
                lookup_id,
            ),
        )


def fail_job(database: Database, job: Job, failure: str, now: datetime) -> None:
    """running の仕事を failed にして失敗の種類を残す。"""
    if job.kind == "answer":
        sql = (
            "UPDATE advice_messages SET status='failed', failure=?, answered_at=?"
            " WHERE id=? AND status='running'"
        )
    else:
        sql = (
            "UPDATE food_lookups SET status='failed', failure=?, finished_at=?"
            " WHERE id=? AND status='running'"
        )
    with database.connect() as db:
        db.execute(sql, (failure, _stamp(now), job.id))


def waiting_position(database: Database, message_id: str) -> int:
    """この回答より前に待っている仕事の数（回答もカロリー探しも数える）。"""
    with database.connect() as db:
        mine = db.execute(
            "SELECT julianday(created_at) AS at FROM advice_messages WHERE id=?",
            (message_id,),
        ).fetchone()
        if mine is None:
            return 0
        answers = db.execute(
            """SELECT COUNT(*) FROM advice_messages m
            JOIN advice_threads t ON t.id = m.thread_id
            WHERE m.role='assistant' AND m.status='queued' AND t.deleted_at IS NULL
            AND (julianday(m.created_at) < ? OR (julianday(m.created_at) = ? AND m.id < ?))""",
            (mine["at"], mine["at"], message_id),
        ).fetchone()[0]
        lookups = db.execute(
            """SELECT COUNT(*) FROM food_lookups
            WHERE status='queued' AND julianday(requested_at) < ?""",
            (mine["at"],),
        ).fetchone()[0]
    return int(answers + lookups)
