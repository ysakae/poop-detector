"""相談の回答とカロリー探しの仕事の取り出し・上限・取り残しの整理を検証する。"""

import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest

from poop_detector.features.advice import jobs
from poop_detector.features.advice.jobs import Job, LookupResult
from poop_detector.records import advice
from poop_detector.records.database import Database

T0 = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "e.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO users (id, name, role, created_at) VALUES ('a', '母', 'admin', '2026')"
        )
    return db


def start(database, now):
    thread = advice.start_thread(database, title="t", question="q", user_id="a", basis="b", now=now)
    with database.connect() as conn:
        row = conn.execute(
            "SELECT id FROM advice_messages WHERE thread_id=? AND role='assistant'", (thread,)
        ).fetchone()
    return thread, row["id"]


def lookup(database, at):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO food_lookups (id, name, status, requested_at)"
            " VALUES ('L', 'フード', 'queued', ?)",
            (at.isoformat(),),
        )
    return "L"


TABLES = {
    "advice_messages": "SELECT * FROM advice_messages WHERE id=?",
    "food_lookups": "SELECT * FROM food_lookups WHERE id=?",
}


def row(database, table, key):
    with database.connect() as conn:
        return conn.execute(TABLES[table], (key,)).fetchone()


def test_claim_next_takes_oldest_across_answers_and_lookups(database):
    _, message = start(database, T0 + timedelta(minutes=1))
    lookup(database, T0)
    first = jobs.claim_next(database, T0 + timedelta(minutes=5), 0)
    assert first == Job("lookup", "L")
    assert row(database, "food_lookups", "L")["status"] == "running"
    assert row(database, "food_lookups", "L")["started_at"] is not None
    assert jobs.claim_next(database, T0 + timedelta(minutes=5), 0) == Job("answer", message)
    assert row(database, "advice_messages", message)["status"] == "running"
    assert jobs.claim_next(database, T0 + timedelta(minutes=5), 0) is None


def test_claim_next_compares_times_across_offsets(database):
    _, message = start(database, T0)
    jst = timezone(timedelta(hours=9))
    # 同じ時刻の直前（UTC 8:59）を +09:00 で表すと文字列では後ろに並ぶ
    lookup(database, (T0 - timedelta(minutes=1)).astimezone(jst))
    assert jobs.claim_next(database, T0, 0) == Job("lookup", "L")
    assert message


def test_claim_next_respects_hourly_limit(database):
    start(database, T0)
    assert jobs.claim_next(database, T0, 20) is None
    assert jobs.claim_next(database, T0, 19) is not None


def test_claim_next_skips_deleted_thread(database):
    thread, message = start(database, T0)
    with database.connect() as conn:
        conn.execute("UPDATE advice_threads SET deleted_at=? WHERE id=?", (T0.isoformat(), thread))
    assert jobs.claim_next(database, T0, 0) is None
    assert row(database, "advice_messages", message)["status"] == "queued"
    lookup(database, T0 + timedelta(minutes=1))
    assert jobs.claim_next(database, T0, 0) == Job("lookup", "L")


def test_two_claimers_never_take_same_job(database):
    start(database, T0)
    others = [Database(database.path) for _ in range(2)]
    barrier = threading.Barrier(2)
    results = []

    def run(db):
        barrier.wait()
        results.append(jobs.claim_next(db, T0, 0))

    threads = [threading.Thread(target=run, args=(db,)) for db in others]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r is None for r in results) == [False, True]


def test_expire_stale_fails_running_rows_after_timeout(database):
    _, message = start(database, T0)
    lookup(database, T0)
    jobs.claim_next(database, T0, 0)
    jobs.claim_next(database, T0, 0)
    edge = T0 + timedelta(seconds=jobs.STALE_SECONDS)
    assert jobs.expire_stale(database, edge) == 0
    assert jobs.expire_stale(database, edge + timedelta(seconds=1)) == 2
    assert row(database, "advice_messages", message)["status"] == "failed"
    assert row(database, "advice_messages", message)["failure"] == "timeout"
    assert row(database, "food_lookups", "L")["failure"] == "timeout"
    assert row(database, "food_lookups", "L")["finished_at"] is not None


def test_finish_after_expire_does_not_overwrite(database):
    _, message = start(database, T0)
    lookup(database, T0)
    jobs.claim_next(database, T0, 0)
    jobs.claim_next(database, T0, 0)
    jobs.expire_stale(database, T0 + timedelta(hours=1))
    jobs.finish_answer(database, message, "遅い回答", "codex", None, T0 + timedelta(hours=2))
    result = LookupResult("n", "m", 3800, "https://e.example", "x")
    jobs.finish_lookup(database, "L", result, T0 + timedelta(hours=2))
    assert row(database, "advice_messages", message)["status"] == "failed"
    assert row(database, "advice_messages", message)["body"] == ""
    assert row(database, "food_lookups", "L")["status"] == "failed"
    assert row(database, "food_lookups", "L")["product_name"] == ""


def test_finish_answer_and_lookup_store_results(database):
    _, message = start(database, T0)
    jobs.claim_next(database, T0, 0)
    jobs.finish_answer(database, message, "回答", "codex", "m1", T0)
    done = row(database, "advice_messages", message)
    assert (done["status"], done["body"], done["provider"], done["model"]) == (
        "done",
        "回答",
        "codex",
        "m1",
    )
    assert done["answered_at"] is not None
    lookup(database, T0)
    jobs.claim_next(database, T0, 0)
    jobs.finish_lookup(
        database, "L", LookupResult("商品", "社", None, "https://e.example", "メモ"), T0
    )
    found = row(database, "food_lookups", "L")
    assert (found["status"], found["product_name"], found["kcal_dk"]) == ("done", "商品", None)
    assert found["finished_at"] is not None
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 0


def test_fail_job_marks_running_rows(database):
    _, message = start(database, T0)
    lookup(database, T0)
    answer = jobs.claim_next(database, T0, 0)
    look = jobs.claim_next(database, T0, 0)
    jobs.fail_job(database, answer, "unavailable", T0)
    jobs.fail_job(database, look, "bad_output", T0)
    assert row(database, "advice_messages", message)["failure"] == "unavailable"
    assert row(database, "advice_messages", message)["answered_at"] is not None
    assert row(database, "food_lookups", "L")["failure"] == "bad_output"
    assert row(database, "food_lookups", "L")["finished_at"] is not None


def test_waiting_position_counts_queued_before(database):
    _, first = start(database, T0)
    _, second = start(database, T0 + timedelta(minutes=1))
    _, third = start(database, T0 + timedelta(minutes=2))
    assert jobs.waiting_position(database, first) == 0
    assert jobs.waiting_position(database, third) == 2
    jobs.claim_next(database, T0, 0)
    assert jobs.waiting_position(database, third) == 1
    assert jobs.waiting_position(database, second) == 0
