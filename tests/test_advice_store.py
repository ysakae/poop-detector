"""相談の会話の台帳（家族で共有）を検証する。"""

from datetime import UTC, datetime, timedelta

import pytest

from poop_detector.errors import InvalidInputError
from poop_detector.records import advice
from poop_detector.records.database import Database

T0 = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "e.sqlite3")
    with db.connect() as conn:
        for uid, name, role in (("a", "母", "admin"), ("b", "父", "member")):
            conn.execute(
                "INSERT INTO users (id, name, role, created_at) VALUES (?, ?, ?, '2026')",
                (uid, name, role),
            )
    return db


def start(database, **extra):
    return advice.start_thread(
        database, title="ごはん", question="量は？", user_id="a", basis="根拠", **extra
    )


def set_status(database, thread_id, status):
    with database.connect() as conn:
        conn.execute(
            "UPDATE advice_messages SET status=?, failure=? WHERE thread_id=? AND role='assistant'",
            (status, "x" if status == "failed" else None, thread_id),
        )


def test_start_thread_queues_answer(database):
    thread_id = start(database, now=T0)
    msgs = advice.messages(database, thread_id)
    assert [(m.seq, m.role, m.status, m.author_name) for m in msgs] == [
        (1, "user", "done", "母"),
        (2, "assistant", "queued", ""),
    ]
    assert msgs[0].body == "量は？"
    assert msgs[0].created_at == T0
    listed = advice.threads(database)
    assert listed[0].state == "queued"
    assert listed[0].author_name == "母"
    assert advice.thread(database, thread_id) == listed[0]
    assert advice.thread(database, "none") is None


def test_validation_messages(database):
    for title, question, message in (
        ("", "q", "題名は60文字以内"),
        ("  ", "q", "題名は60文字以内"),
        ("x" * 61, "q", "題名は60文字以内"),
        ("t", " ", "質問は2000文字以内"),
        ("t", "q" * 2001, "質問は2000文字以内"),
    ):
        with pytest.raises(InvalidInputError, match=message):
            advice.start_thread(database, title=title, question=question, user_id="a", basis="")
    assert advice.threads(database) == []


def test_ask_is_refused_while_answer_pending(database):
    thread_id = start(database)
    for status in ("queued", "running"):
        set_status(database, thread_id, status)
        with pytest.raises(InvalidInputError, match="前の質問の回答を待っています"):
            advice.ask(database, thread_id, question="もう一つ", user_id="b", basis="")
    assert len(advice.messages(database, thread_id)) == 2


def test_ask_appends_after_answer(database):
    thread_id = start(database, now=T0)
    set_status(database, thread_id, "done")
    advice.ask(
        database, thread_id, question="  次は？ ", user_id="b", basis="b", now=T0 + timedelta(1)
    )
    msgs = advice.messages(database, thread_id)
    assert [(m.seq, m.role, m.status) for m in msgs[2:]] == [
        (3, "user", "done"),
        (4, "assistant", "queued"),
    ]
    assert msgs[2].body == "次は？"
    assert msgs[2].author_name == "父"
    assert advice.threads(database)[0].updated_at == T0 + timedelta(1)
    with pytest.raises(InvalidInputError, match="質問は2000文字以内"):
        advice.ask(database, thread_id, question=" ", user_id="b", basis="")


def test_ask_after_failed_answer_is_allowed(database):
    thread_id = start(database)
    set_status(database, thread_id, "failed")
    advice.ask(database, thread_id, question="q", user_id="b", basis="")
    assert len(advice.messages(database, thread_id)) == 4


def test_ask_unknown_thread(database):
    with pytest.raises(InvalidInputError, match="相談が見つかりません"):
        advice.ask(database, "none", question="q", user_id="a", basis="")


def test_retry_requeues_only_failed_answer(database):
    thread_id = start(database)
    answer = advice.messages(database, thread_id)[1]
    with pytest.raises(InvalidInputError, match="やり直す回答が見つかりません"):
        advice.retry(database, answer.id)
    set_status(database, thread_id, "failed")
    assert advice.messages(database, thread_id)[1].failure == "x"
    advice.retry(database, answer.id)
    again = advice.messages(database, thread_id)[1]
    assert (again.status, again.failure) == ("queued", None)
    with pytest.raises(InvalidInputError, match="やり直す回答が見つかりません"):
        advice.retry(database, "none")


def test_delete_thread_only_by_creator_or_admin(database):
    mine = start(database)
    with pytest.raises(InvalidInputError, match="始めた人か管理者だけ"):
        advice.delete_thread(database, mine, user_id="b", admin=False)
    advice.delete_thread(database, mine, user_id="a", admin=False)
    assert advice.threads(database) == []
    assert advice.thread(database, mine) is None
    with pytest.raises(InvalidInputError, match="相談が見つかりません"):
        advice.ask(database, mine, question="q", user_id="a", basis="")
    with pytest.raises(InvalidInputError, match="相談が見つかりません"):
        advice.delete_thread(database, mine, user_id="a", admin=True)
    other = start(database)
    advice.delete_thread(database, other, user_id="b", admin=True)
    assert advice.threads(database) == []


def test_threads_newest_first(database):
    first = start(database, now=T0)
    second = start(database, now=T0 + timedelta(1))
    assert [t.id for t in advice.threads(database)] == [second, first]
