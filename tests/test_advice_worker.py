"""相談の作業スレッドの処理（回答・カロリー探し・失敗・上限）を検証する。"""

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from support import FakeAdvisor, NoLookupAdvisor

from poop_detector.features.advice.worker import AdviceWorker
from poop_detector.gui.advice_worker import configured_judge as real_configured_judge
from poop_detector.records import advice
from poop_detector.records.events import Store
from poop_detector.video.judge_failures import JudgeCallError
from poop_detector.video.judge_runs import JudgeRunLog

NOW = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    database = store.database
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO users (id, name, role, created_at) VALUES ('a', '母', 'admin', '2026')"
        )
    return store, database


def make_worker(setup, tmp_path, judge):
    store, database = setup
    return AdviceWorker(
        database,
        store,
        lambda: (judge, "codex", "m"),
        lambda: "ポチ",
        tmp_path / "work",
        clock=lambda: NOW,
    )


def ask(database):
    thread = advice.start_thread(
        database, title="t", question="量は？", user_id="a", basis="{}", now=NOW
    )
    return thread, advice.messages(database, thread)[-1].id


def answer_row(database, message_id):
    with database.connect() as conn:
        return conn.execute("SELECT * FROM advice_messages WHERE id=?", (message_id,)).fetchone()


def runs(database, purpose):
    with database.connect() as conn:
        return conn.execute("SELECT * FROM judge_runs WHERE purpose=?", (purpose,)).fetchall()


def queue_lookup(database):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO food_lookups (id, name, status, requested_at)"
            " VALUES ('L', '○○パピー', 'queued', ?)",
            (NOW.isoformat(),),
        )


def lookup_row(database):
    with database.connect() as conn:
        return conn.execute("SELECT * FROM food_lookups WHERE id='L'").fetchone()


def test_worker_answers_queued_question_and_records_run(setup, tmp_path):
    _, database = setup
    _, message = ask(database)
    judge = FakeAdvisor()
    assert make_worker(setup, tmp_path, judge).poll_once() is True
    row = answer_row(database, message)
    assert row["status"] == "done"
    assert row["body"] == "量は今のままで大丈夫です"
    assert len(runs(database, "advice")) == 1
    assert "量は？" in judge.prompts[0]
    assert not list((tmp_path / "work").iterdir())


def test_worker_returns_false_without_jobs(setup, tmp_path):
    assert make_worker(setup, tmp_path, FakeAdvisor()).poll_once() is False


def test_worker_marks_failure_kind_and_allows_retry(setup, tmp_path):
    _, database = setup
    _, message = ask(database)
    make_worker(setup, tmp_path, FakeAdvisor(answer=JudgeCallError("x", "timeout"))).poll_once()
    row = answer_row(database, message)
    assert (row["status"], row["failure"]) == ("failed", "timeout")
    assert runs(database, "advice")[0]["failed"] == 1
    advice.retry(database, message)
    make_worker(setup, tmp_path, FakeAdvisor()).poll_once()
    assert answer_row(database, message)["status"] == "done"


def test_worker_counts_unparsable_output_as_invalid(setup, tmp_path):
    _, database = setup
    _, message = ask(database)
    make_worker(setup, tmp_path, FakeAdvisor(answer={"answer": ""})).poll_once()
    row = answer_row(database, message)
    assert (row["status"], row["failure"]) == ("failed", "invalid_output")
    assert runs(database, "advice")[0]["failure"] == "invalid_output"


def test_worker_lookup_drafts_result(setup, tmp_path):
    _, database = setup
    queue_lookup(database)
    output = {
        "product_name": "○○パピー",
        "maker": "○○",
        "kcal_per_100g": 385,
        "source_url": "https://maker.example/p",
        "note": "",
    }
    assert make_worker(setup, tmp_path, FakeAdvisor(lookup_output=output)).poll_once() is True
    row = lookup_row(database)
    assert (row["status"], row["kcal_dk"]) == ("done", 3850)
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 0
    assert len(runs(database, "food_lookup")) == 1


def test_worker_fails_lookup_when_judge_has_no_web_search(setup, tmp_path):
    _, database = setup
    queue_lookup(database)
    make_worker(setup, tmp_path, NoLookupAdvisor()).poll_once()
    row = lookup_row(database)
    assert (row["status"], row["failure"]) == ("failed", "unsupported")
    assert runs(database, "food_lookup") == []


def test_worker_waits_when_hourly_limit_reached(setup, tmp_path):
    _, database = setup
    _, message = ask(database)
    log = JudgeRunLog(database)
    for _ in range(20):
        log.record("codex", None, "advice", failed=False, at=NOW - timedelta(minutes=5))
    assert make_worker(setup, tmp_path, FakeAdvisor()).poll_once() is False
    assert answer_row(database, message)["status"] == "queued"


def test_review_runs_do_not_block_advice(setup, tmp_path):
    _, database = setup
    _, message = ask(database)
    log = JudgeRunLog(database)
    for _ in range(100):
        log.record("codex", None, "review", failed=False, at=NOW - timedelta(minutes=5))
    assert make_worker(setup, tmp_path, FakeAdvisor()).poll_once() is True
    assert answer_row(database, message)["status"] == "done"


def test_gui_server_starts_and_stops_advice_worker(gui):
    server, _, _ = gui
    database = server.app.store.database
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO users (id, name, role, created_at) VALUES ('a', '母', 'admin', '2026')"
        )
    _, message = ask(database)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if answer_row(database, message)["status"] == "done":
            break
        time.sleep(0.1)
    assert answer_row(database, message)["status"] == "done"
    server.shutdown()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(
        t.name == "advice-worker" for t in threading.enumerate()
    ):
        time.sleep(0.05)
    assert not any(t.name == "advice-worker" for t in threading.enumerate())


@pytest.mark.parametrize("broken", ["choose", "pet_name"])
def test_unexpected_error_fails_job_as_other(setup, tmp_path, broken):
    _, database = setup
    _, message = ask(database)

    def boom():
        raise RuntimeError("壊れた")

    worker = make_worker(setup, tmp_path, FakeAdvisor())
    setattr(worker, broken, boom)
    assert worker.poll_once() is True
    row = answer_row(database, message)
    assert (row["status"], row["failure"]) == ("failed", "other")
    assert runs(database, "advice") == []


def test_configured_judge_reads_settings(tmp_path):
    from poop_detector.video.codex_cli import CodexCliJudge

    config = tmp_path / "config.yaml"
    config.write_text("review:\n  provider: codex\n  model: gpt-test\n")
    judge, provider, model = real_configured_judge(config)
    assert isinstance(judge, CodexCliJudge)
    assert (provider, model) == ("codex", "gpt-test")
    config.write_text("review:\n  provider: codex\n  model: ''\n")
    assert real_configured_judge(config)[2] is None
