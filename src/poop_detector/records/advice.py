"""家族で共有するごはんの相談の会話の台帳。回答は後から別の処理が埋める。"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from ..errors import InvalidInputError
from ..timezones import local_now
from .database import Database

MAX_TITLE_LENGTH = 60
MAX_QUESTION_LENGTH = 2000
TITLE_ERROR = f"題名は{MAX_TITLE_LENGTH}文字以内で入力してください"
QUESTION_ERROR = f"質問は{MAX_QUESTION_LENGTH}文字以内で入力してください"
PENDING_ERROR = "前の質問の回答を待っています"
MISSING_ERROR = "相談が見つかりません"


@dataclass(frozen=True)
class Message:
    """会話の1件。質問（user）か回答（assistant）で、回答は status が進む。"""

    id: str
    thread_id: str
    seq: int
    role: Literal["user", "assistant"]
    author_name: str
    body: str
    status: str
    failure: str | None
    created_at: datetime


@dataclass(frozen=True)
class Thread:
    """会話の一覧の1行。state は最後の回答の status。"""

    id: str
    title: str
    created_by: str | None
    author_name: str
    updated_at: datetime
    state: str


def _stamp(now: datetime | None) -> str:
    return (now or local_now()).astimezone(UTC).isoformat()


def _clean(value: str, limit: int, error: str) -> str:
    text = value.strip()
    if not 1 <= len(text) <= limit:
        raise InvalidInputError(error)
    return text


def _append(
    db: sqlite3.Connection, thread_id: str, question: str, user_id: str, basis: str, stamp: str
) -> None:
    """質問と待ちの回答を足す。呼び出し側が BEGIN IMMEDIATE の中で呼ぶ。"""
    seq = db.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM advice_messages WHERE thread_id=?", (thread_id,)
    ).fetchone()[0]
    db.execute(
        """INSERT INTO advice_messages (id, thread_id, seq, role, author, body, status, created_at)
        VALUES (?, ?, ?, 'user', ?, ?, 'done', ?)""",
        (uuid4().hex, thread_id, seq + 1, user_id, question, stamp),
    )
    db.execute(
        """INSERT INTO advice_messages (id, thread_id, seq, role, status, basis, created_at)
        VALUES (?, ?, ?, 'assistant', 'queued', ?, ?)""",
        (uuid4().hex, thread_id, seq + 2, basis, stamp),
    )
    db.execute("UPDATE advice_threads SET updated_at=? WHERE id=?", (stamp, thread_id))


def start_thread(
    database: Database,
    *,
    title: str,
    question: str,
    user_id: str,
    basis: str,
    now: datetime | None = None,
) -> str:
    """会話を始め、最初の質問と待ちの回答を同時に入れる。"""
    title = _clean(title, MAX_TITLE_LENGTH, TITLE_ERROR)
    question = _clean(question, MAX_QUESTION_LENGTH, QUESTION_ERROR)
    stamp = _stamp(now)
    thread_id = uuid4().hex
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """INSERT INTO advice_threads (id, title, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)""",
            (thread_id, title, user_id, stamp, stamp),
        )
        _append(db, thread_id, question, user_id, basis, stamp)
    return thread_id


def ask(
    database: Database,
    thread_id: str,
    *,
    question: str,
    user_id: str,
    basis: str,
    now: datetime | None = None,
) -> None:
    """質問を足す。回答待ちの確認と挿入は1つのトランザクションで行い、同時の質問を防ぐ。"""
    question = _clean(question, MAX_QUESTION_LENGTH, QUESTION_ERROR)
    stamp = _stamp(now)
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        found = db.execute(
            "SELECT 1 FROM advice_threads WHERE id=? AND deleted_at IS NULL", (thread_id,)
        ).fetchone()
        if found is None:
            raise InvalidInputError(MISSING_ERROR)
        pending = db.execute(
            """SELECT 1 FROM advice_messages
            WHERE thread_id=? AND role='assistant' AND status IN ('queued', 'running')""",
            (thread_id,),
        ).fetchone()
        if pending is not None:
            raise InvalidInputError(PENDING_ERROR)
        _append(db, thread_id, question, user_id, basis, stamp)


def retry(database: Database, message_id: str) -> None:
    """失敗した回答だけを待ちに戻す。"""
    with database.connect() as db:
        changed = db.execute(
            """UPDATE advice_messages SET status='queued', failure=NULL, started_at=NULL
            WHERE id=? AND role='assistant' AND status='failed'""",
            (message_id,),
        ).rowcount
    if not changed:
        raise InvalidInputError("やり直す回答が見つかりません")


_THREAD_SQL = """SELECT t.id, t.title, t.created_by, COALESCE(u.name, '') AS author_name,
    t.updated_at,
    COALESCE((SELECT m.status FROM advice_messages m
        WHERE m.thread_id=t.id AND m.role='assistant' ORDER BY m.seq DESC LIMIT 1), '') AS state
    FROM advice_threads t LEFT JOIN users u ON u.id=t.created_by
    WHERE t.deleted_at IS NULL"""


def _thread(row: sqlite3.Row) -> Thread:
    return Thread(
        row["id"],
        row["title"],
        row["created_by"],
        row["author_name"],
        datetime.fromisoformat(row["updated_at"]),
        row["state"],
    )


def threads(database: Database) -> list[Thread]:
    """削除していない会話を、更新の新しい順に返す。"""
    with database.connect() as db:
        rows = db.execute(f"{_THREAD_SQL} ORDER BY t.updated_at DESC, t.rowid DESC").fetchall()
    return [_thread(row) for row in rows]


def thread(database: Database, thread_id: str) -> Thread | None:
    with database.connect() as db:
        row = db.execute(f"{_THREAD_SQL} AND t.id=?", (thread_id,)).fetchone()
    return None if row is None else _thread(row)


def messages(database: Database, thread_id: str) -> list[Message]:
    with database.connect() as db:
        rows = db.execute(
            """SELECT m.id, m.thread_id, m.seq, m.role, COALESCE(u.name, '') AS author_name,
                m.body, m.status, m.failure, m.created_at
            FROM advice_messages m LEFT JOIN users u ON u.id=m.author
            WHERE m.thread_id=? ORDER BY m.seq""",
            (thread_id,),
        ).fetchall()
    return [
        Message(
            row["id"],
            row["thread_id"],
            row["seq"],
            row["role"],
            row["author_name"],
            row["body"],
            row["status"],
            row["failure"],
            datetime.fromisoformat(row["created_at"]),
        )
        for row in rows
    ]


def delete_thread(database: Database, thread_id: str, *, user_id: str, admin: bool) -> None:
    """会話を消す（印を付けるだけ）。消せるのは始めた人か管理者。"""
    stamp = _stamp(None)
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT created_by FROM advice_threads WHERE id=? AND deleted_at IS NULL",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise InvalidInputError(MISSING_ERROR)
        if not admin and row["created_by"] != user_id:
            raise InvalidInputError("この相談を消せるのは、始めた人か管理者だけです")
        db.execute("UPDATE advice_threads SET deleted_at=? WHERE id=?", (stamp, thread_id))
