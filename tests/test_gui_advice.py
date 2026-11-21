"""フードの画面（カロリー探しと手入力）を検証する。"""

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from support import exchange, login, request, submit

from poop_detector.features.advice.jobs import LookupResult, finish_lookup
from poop_detector.records.foods import active_foods, recent_lookups

PAGE = "/advice/foods"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
URL = "https://example.com/puppy"


@pytest.fixture
def judge_ready(monkeypatch):
    monkeypatch.setattr("poop_detector.video.codex_cli.CodexCliJudge.check", lambda self: None)


def test_food_lookup_draft_then_confirm(gui, judge_ready):
    server, store, _ = gui
    assert "カロリーを探す</button>" in request(server, "GET", PAGE)[1].decode()
    assert submit(server, "/advice/foods/lookup", {"name": "○○パピー"}, page=PAGE)[0] == 303
    (lookup,) = recent_lookups(store.database)
    assert lookup.status == "queued"
    now = datetime.now(UTC)
    with store.database.connect() as db:
        db.execute(
            "UPDATE food_lookups SET status='running', started_at=? WHERE id=?",
            (now.isoformat(), lookup.id),
        )
    finish_lookup(
        store.database, lookup.id, LookupResult("○○パピー", "○○社", 3855, URL, "袋の表示"), now
    )
    html = request(server, "GET", PAGE)[1].decode()
    assert "385.5" in html
    assert f'<a href="{URL}" rel="noopener noreferrer" target="_blank">{URL}</a>' in html
    fields = {"name": "○○パピー", "kcal": "385.5", "source_url": URL}
    assert submit(server, PAGE, fields, page=PAGE)[0] == 303
    (food,) = active_foods(store.database)
    assert (food.name, food.kcal_dk, food.source_url) == ("○○パピー", 3855, URL)
    assert "385.5 kcal/100g" in request(server, "GET", PAGE)[1].decode()
    assert submit(server, "/advice/foods/stop", {"food_id": food.id}, page=PAGE)[0] == 303
    assert active_foods(store.database) == []


def test_food_manual_entry_errors_keep_input(gui):
    server, _, _ = gui
    status, body = submit(server, PAGE, {"name": "手入力フード", "kcal": "900"}, page=PAGE)
    assert status == 400
    html = body.decode()
    assert 'value="手入力フード"' in html
    assert "30〜700" in html


def test_food_lookup_hidden_when_judge_unavailable(gui, monkeypatch):
    def check(self):
        raise ValueError("codex loginでCodex CLIにログインしてください")

    monkeypatch.setattr("poop_detector.video.codex_cli.CodexCliJudge.check", check)
    server, _, _ = gui
    html = request(server, "GET", PAGE)[1].decode()
    assert "codex login" in html
    assert 'action="/advice/foods/lookup"' not in html


def test_food_lookup_failure_is_shown_in_japanese(gui, judge_ready):
    server, store, _ = gui
    submit(server, "/advice/foods/lookup", {"name": "失敗フード"}, page=PAGE)
    with store.database.connect() as db:
        db.execute("UPDATE food_lookups SET status='failed', failure='timeout'")
    assert "時間切れ" in request(server, "GET", PAGE)[1].decode()


def test_advice_foods_requires_login(gui):
    server, _, _ = gui
    assert request(server, "GET", PAGE, auth=False)[0] == 403


def test_lookup_draft_text_is_escaped_and_unsafe_url_is_not_linked(gui, judge_ready):
    server, store, _ = gui
    hostile = "<script>\"&'"
    with store.database.connect() as db:
        db.execute(
            """INSERT INTO food_lookups (id, name, status, product_name, maker, kcal_dk,
            source_url, note, requested_at) VALUES ('x', 'n', 'done', ?, ?, 3850,
            'javascript:alert(1)', ?, '2026-11-21T00:00:00+00:00')""",
            (hostile, hostile, hostile),
        )
    html = request(server, "GET", PAGE)[1].decode()
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "javascript:" not in html


def test_food_post_without_csrf_is_refused(gui):
    server, _, _ = gui
    body = urlencode({"name": "x", "kcal": "385"})
    assert request(server, "POST", PAGE, body, FORM)[0] == 403
    assert request(server, "POST", PAGE, urlencode({"csrf": "wrong"}), FORM)[0] == 403


def test_empty_lookup_name_keeps_form_and_returns_400(gui, judge_ready):
    server, _, _ = gui
    status, body = submit(server, "/advice/foods/lookup", {"name": "   "}, page=PAGE)
    assert status == 400
    assert 'action="/advice/foods/lookup"' in body.decode()


# --- 目安・プロフィール・相談 ---

ADVICE = "/advice"


def _first_user(store):
    with store.database.connect() as db:
        return db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]


def _setup_guide(server, store, *, birth=True, food_active=True):
    """生後5か月・3.2 kg・小型（予想5 kg）・385 kcal のフード・給餌の予定3件を入れる。"""
    from datetime import timedelta

    from poop_detector.records.feeding_plans import save_plan
    from poop_detector.records.foods import active_foods, save_food, stop_food
    from poop_detector.records.profile import save_profile
    from poop_detector.records.weights import save_birth_date, save_weight
    from poop_detector.timezones import local_today

    request(server, "GET", "/")  # セッションのユーザーを作る
    today = local_today()
    database = store.database
    if birth:
        save_birth_date(database, (today - timedelta(days=152)).isoformat())
    save_weight(database, "", today, 3200, "")
    save_food(database, name="パピー", kcal_dk=3850, source_url="", user_id=_first_user(store))
    (food,) = active_foods(database)
    save_profile(
        database,
        breed="",
        size_class="small",
        adult_grams=5000,
        neutered="",
        food_id=food.id,
    )
    for clock in ("08:00", "12:00", "18:00"):
        save_plan(database, clock)
    if not food_active:
        stop_food(database, food.id)


def test_advice_page_shows_guide_from_records(gui):
    server, store, _ = gui
    _setup_guide(server, store)
    html = request(server, "GET", ADVICE)[1].decode()
    assert "109 g" in html
    assert "36 g" in html
    assert "獣医師の指示があればそちらを優先してください" in html


def test_advice_page_guides_missing_inputs(gui):
    server, store, _ = gui
    _setup_guide(server, store, birth=False)
    html = request(server, "GET", ADVICE)[1].decode()
    assert "生年月日を入れてください" in html
    assert "109 g" not in html


def test_inactive_profile_food_is_treated_as_none(gui):
    server, store, _ = gui
    _setup_guide(server, store, food_active=False)
    html = request(server, "GET", ADVICE)[1].decode()
    assert "フードのカロリーが分からないので" in html
    assert "109 g" not in html


def test_profile_breed_fills_size_and_adult_weight(gui):
    from poop_detector.records.profile import load_profile

    server, store, _ = gui
    fields = {
        "breed": "チワワ",
        "size_class": "",
        "adult_grams": "",
        "neutered": "1",
        "food_id": "",
    }
    assert submit(server, "/advice/profile", fields, page=ADVICE)[0] == 303
    profile = load_profile(store.database)
    assert (profile.size_class, profile.adult_grams, profile.neutered) == ("toy", 2500, True)
    # 手で入れた値は表で上書きしない。
    fields = {**fields, "size_class": "medium", "adult_grams": "12000"}
    assert submit(server, "/advice/profile", fields, page=ADVICE)[0] == 303
    profile = load_profile(store.database)
    assert (profile.size_class, profile.adult_grams) == ("medium", 12000)


def test_profile_invalid_weight_returns_400(gui):
    server, _, _ = gui
    fields = {"breed": "", "size_class": "", "adult_grams": "abc", "neutered": "", "food_id": ""}
    status, body = submit(server, "/advice/profile", fields, page=ADVICE)
    assert status == 400
    assert 'name="adult_grams" inputmode="numeric" value="abc"' in body.decode()


def _wait_for(server, path, text):
    import time

    deadline = time.monotonic() + 10
    html = ""
    while time.monotonic() < deadline:
        html = request(server, "GET", path)[1].decode()
        if text in html:
            return html
        time.sleep(0.1)
    raise AssertionError(f"{text!r} が出ませんでした: {html[:300]}")


def _thread_id(store):
    from poop_detector.records.advice import threads

    return threads(store.database)[0].id


def test_question_round_trip(gui, judge_ready):
    server, store, _ = gui
    request(server, "GET", ADVICE)
    status, _ = submit(
        server, "/advice/threads", {"title": "量の相談", "question": "今の量でいい？"}, page=ADVICE
    )
    assert status == 303
    thread_id = _thread_id(store)
    page = f"/advice/thread?id={thread_id}"
    first = request(server, "GET", page)[1].decode()
    assert "今の量でいい？" in first
    html = _wait_for(server, page, "量は今のままで大丈夫です")
    assert "回答を作っています" not in html
    status, headers, body = exchange(server, "GET", f"/advice/status?thread={thread_id}")
    assert status == 200
    assert json.loads(body) == {"state": "done"}
    assert "no-store" in headers["Cache-Control"]


def test_waiting_text_and_form_hidden_while_pending(gui, judge_ready, monkeypatch):
    from poop_detector.records.advice import start_thread

    server, store, _ = gui
    request(server, "GET", ADVICE)
    thread_id = start_thread(
        store.database, title="待ち", question="q", user_id=_first_user(store), basis="{}"
    )
    # 作業スレッドに取られないよう、処理中の状態に固定する。
    with store.database.connect() as db:
        db.execute("UPDATE advice_messages SET status='running' WHERE role='assistant'")
    html = request(server, "GET", f"/advice/thread?id={thread_id}")[1].decode()
    assert "回答を作っています" in html
    assert 'action="/advice/ask"' not in html
    assert "data-advice-status" in html


def test_second_question_refused_while_pending(gui, judge_ready):
    from poop_detector.records.advice import start_thread

    server, store, _ = gui
    request(server, "GET", ADVICE)
    thread_id = start_thread(
        store.database, title="待ち", question="q", user_id=_first_user(store), basis="{}"
    )
    with store.database.connect() as db:
        db.execute("UPDATE advice_messages SET status='running' WHERE role='assistant'")
    status, body = submit(
        server,
        "/advice/ask",
        {"thread_id": thread_id, "question": "もう一つ"},
        page=f"/advice/thread?id={thread_id}",
    )
    assert status == 400
    assert "前の質問の回答を待っています" in body.decode()


def test_failed_answer_shows_reason_and_retry(gui, judge_ready):
    from poop_detector.records.advice import start_thread

    server, store, _ = gui
    request(server, "GET", ADVICE)
    thread_id = start_thread(
        store.database, title="失敗", question="q", user_id=_first_user(store), basis="{}"
    )
    with store.database.connect() as db:
        db.execute("UPDATE advice_messages SET status='failed', failure='timeout' WHERE seq=2")
        message = db.execute("SELECT id FROM advice_messages WHERE seq=2").fetchone()[0]
    page = f"/advice/thread?id={thread_id}"
    html = request(server, "GET", page)[1].decode()
    assert "時間切れでした" in html
    assert "もう一度聞く" in html
    assert submit(server, "/advice/retry", {"message_id": message}, page=page)[0] == 303
    with store.database.connect() as db:
        status = db.execute("SELECT status FROM advice_messages WHERE id=?", (message,)).fetchone()
    assert status[0] in ("queued", "running", "done")


def test_delete_thread_permissions(gui, judge_ready):
    from poop_detector.records.advice import start_thread, threads

    server, store, _ = gui
    request(server, "GET", ADVICE)
    owner = _first_user(store)
    thread_id = start_thread(store.database, title="消す", question="q", user_id=owner, basis="{}")
    member = login(server, role="member")
    page = f"/advice/thread?id={thread_id}"
    html = exchange(server, "GET", page, headers=member, auth=False)[2].decode()
    assert 'action="/advice/delete"' not in html
    page_html = exchange(server, "GET", ADVICE, headers=member, auth=False)[2].decode()
    token = re.search(r'name="csrf" value="([^"]+)"', page_html)[1]
    body = urlencode({"csrf": token, "thread_id": thread_id})
    sent = {**member, **FORM}
    assert exchange(server, "POST", "/advice/delete", body, sent, auth=False)[0] == 403
    assert len(threads(store.database)) == 1
    assert submit(server, "/advice/delete", {"thread_id": thread_id}, page=page)[0] == 303
    assert threads(store.database) == []


def test_advice_page_hides_form_when_judge_unavailable(gui, monkeypatch):
    def check(self):
        raise ValueError("codex loginでCodex CLIにログインしてください")

    monkeypatch.setattr("poop_detector.video.codex_cli.CodexCliJudge.check", check)
    server, _, _ = gui
    html = request(server, "GET", ADVICE)[1].decode()
    assert "codex login" in html
    assert 'action="/advice/threads"' not in html


def test_today_grams(gui):
    from poop_detector.gui.advice_page import today_grams
    from poop_detector.timezones import local_today

    server, store, _ = gui
    assert today_grams(store.database, local_today()) is None
    _setup_guide(server, store)
    assert today_grams(store.database, local_today()) == 109


def test_advice_status_for_unknown_thread_is_404(gui):
    server, _, _ = gui
    assert request(server, "GET", "/advice/status?thread=nope")[0] == 404


def test_advice_js_is_served(gui):
    server, _, _ = gui
    assert request(server, "GET", "/assets/advice.js")[0] == 200


def test_retry_on_deleted_thread_is_refused(gui, judge_ready):
    from poop_detector.records.advice import delete_thread, start_thread

    server, store, _ = gui
    request(server, "GET", ADVICE)
    owner = _first_user(store)
    thread_id = start_thread(
        store.database, title="消した", question="q", user_id=owner, basis="{}"
    )
    with store.database.connect() as db:
        db.execute("UPDATE advice_messages SET status='failed', failure='timeout' WHERE seq=2")
        message = db.execute("SELECT id FROM advice_messages WHERE seq=2").fetchone()[0]
    delete_thread(store.database, thread_id, user_id=owner, admin=True)
    status, body = submit(
        server, "/advice/retry", {"message_id": message, "thread_id": thread_id}, page=ADVICE
    )
    assert status == 400
    assert "やり直す回答が見つかりません" in body.decode()
    with store.database.connect() as db:
        row = db.execute("SELECT status FROM advice_messages WHERE id=?", (message,)).fetchone()
    assert row[0] == "failed"


def test_retry_error_renders_thread_page(gui, judge_ready):
    from poop_detector.records.advice import start_thread

    server, store, _ = gui
    request(server, "GET", ADVICE)
    thread_id = start_thread(
        store.database, title="残る", question="q", user_id=_first_user(store), basis="{}"
    )
    with store.database.connect() as db:
        db.execute("UPDATE advice_messages SET status='running' WHERE seq=2")
        message = db.execute("SELECT id FROM advice_messages WHERE seq=2").fetchone()[0]
    status, body = submit(
        server,
        "/advice/retry",
        {"message_id": message, "thread_id": thread_id},
        page=f"/advice/thread?id={thread_id}",
    )
    assert status == 400
    assert "残る" in body.decode()


def test_waiting_position_text(tmp_path):
    from poop_detector.gui.advice_page import message_rows
    from poop_detector.records.advice import messages, start_thread
    from poop_detector.records.database import Database

    database = Database(tmp_path / "events.sqlite3")
    start_thread(database, title="先", question="q", user_id="u", basis="{}")
    later = start_thread(database, title="後", question="q", user_id="u", basis="{}")
    rows = message_rows(database, messages(database, later))
    assert "前に1件" in str(rows[-1]["status_text"])


def test_gain_text_never_shows_negative_zero():
    from datetime import date

    from poop_detector.gui.advice_page import gain_text
    from poop_detector.records.weights import Weight

    today = date(2026, 11, 21)
    rows = [Weight("a", date(2026, 11, 1), 3000, ""), Weight("b", date(2026, 11, 20), 2999, "")]
    assert gain_text(rows, today) == "±0 g/週"
    flat = [Weight("a", date(2026, 11, 1), 3000, ""), Weight("b", date(2026, 11, 20), 3000, "")]
    assert gain_text(flat, today) == "±0 g/週"
