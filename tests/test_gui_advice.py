"""フードの画面（カロリー探しと手入力）を検証する。"""

from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from support import request, submit

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
