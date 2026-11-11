"""GUI のサーバー側のセッション（有効期限、ログアウト）と、要求の回数の制限を検証する。"""

import sqlite3
import threading
from contextlib import contextmanager
from urllib.parse import urlencode

import pytest
from support import exchange, login

from poop_detector.config import Config, ConfigError
from poop_detector.gui.rate_limit import RateLimiter, RateLimits
from poop_detector.gui.server import make_server
from poop_detector.gui.sessions import SessionPolicy, SessionStore
from poop_detector.records.database import events_database

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def sessions(tmp_path):
    clock = Clock()
    store = SessionStore(events_database(tmp_path), SessionPolicy(1, 2), clock=clock)
    return store, clock, tmp_path / "events.sqlite3"


# --- セッションの台帳 ---


def test_session_id_is_random_and_only_its_hash_is_stored(sessions):
    store, _, path = sessions
    first, second = store.create(), store.create()
    assert first != second
    assert len(first) >= 40
    assert store.check(first)
    with sqlite3.connect(path) as db:
        stored = {row[0] for row in db.execute("SELECT id FROM gui_sessions")}
    assert first not in stored
    assert second not in stored
    assert len(stored) == 2


def test_session_expires_after_idle_time(sessions):
    store, clock, _ = sessions
    session = store.create()
    clock.now += 3599
    assert store.check(session)  # 使うと無操作の時間は延びる
    clock.now += 3599
    assert store.check(session)
    clock.now += 3601
    assert not store.check(session)
    clock.now -= 3601
    assert not store.check(session)  # 期限切れのセッションは消える


def test_session_expires_after_its_lifetime_even_when_used(sessions):
    store, clock, _ = sessions
    session = store.create()
    # 無操作の期限（1時間）より短い30分ごとに使っても、2日で切れる。
    for _ in range(95):
        clock.now += 1800
        assert store.check(session)
    clock.now += 1800
    assert not store.check(session)


def test_unknown_and_revoked_sessions_are_rejected(sessions):
    store, _, _ = sessions
    session = store.create()
    assert not store.check("unknown")
    assert not store.check("")
    store.revoke(session)
    assert not store.check(session)


def test_expired_sessions_are_removed_when_new_ones_are_made(sessions):
    store, clock, path = sessions
    store.create()
    clock.now += 7200
    store.create()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM gui_sessions").fetchone()[0] == 1


def test_session_settings_are_validated():
    assert Config(gui_session_idle_hours=24, gui_session_max_days=7)
    with pytest.raises(ConfigError, match=r"gui\.session_idle_hours"):
        Config(gui_session_idle_hours=0)
    with pytest.raises(ConfigError, match=r"gui\.session_max_days"):
        Config(gui_session_max_days=400)


# --- HTTP ---


@contextmanager
def running(root, **kwargs):
    server = make_server(root, 0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_token_url_starts_a_server_side_session(tmp_path):
    with running(tmp_path) as server:
        status, headers, _ = exchange(server, "GET", f"/?token={server.access_token}", auth=False)
        assert status == 303
        cookie = headers["Set-Cookie"]
        value = cookie.split(";", 1)[0].split("=", 1)[1]
        assert value != server.access_token
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert f"Max-Age={30 * 86400}" in cookie
        own = {"Cookie": f"poop_detector_session={value}"}
        assert exchange(server, "GET", "/", headers=own, auth=False)[0] == 200
        # 起動トークンそのものは Cookie として通らない。
        token = {"Cookie": f"poop_detector_session={server.access_token}"}
        assert exchange(server, "GET", "/", headers=token, auth=False)[0] == 403


def test_sessions_survive_a_restart_of_the_gui(tmp_path):
    with running(tmp_path) as server:
        cookie = login(server)
    with running(tmp_path) as server:
        assert exchange(server, "GET", "/", headers=cookie, auth=False)[0] == 200


def test_logout_ends_the_session(tmp_path):
    with running(tmp_path) as server:
        cookie = login(server)
        status, _, page = exchange(server, "GET", "/logout", headers=cookie, auth=False)
        assert status == 200
        assert 'action="/logout"' in page.decode()
        body = urlencode({"csrf": csrf_token_for(server, cookie)})
        status, headers, page = exchange(
            server, "POST", "/logout", body, {**FORM, **cookie}, auth=False
        )
        assert status == 200
        assert "ログアウトしました" in page.decode()
        assert "Max-Age=0" in headers["Set-Cookie"]
        assert exchange(server, "GET", "/", headers=cookie, auth=False)[0] == 403


def test_every_page_links_to_logout(gui):
    server, _, _ = gui
    for path in ("/", "/records", "/clips", "/activity", "/manage"):
        assert b'href="/logout"' in exchange(server, "GET", path)[2]


# --- 回数の制限 ---


def test_rate_limiter_allows_a_burst_then_waits():
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    rule = (3, 60.0)
    assert [limiter.hit("a", "post", rule) for _ in range(3)] == [0, 0, 0]
    wait = limiter.hit("a", "post", rule)
    assert 0 < wait <= 60
    assert limiter.hit("b", "post", rule) == 0  # 接続元ごとに数える
    clock.now += 61
    assert limiter.hit("a", "post", rule) == 0


def test_rate_limiter_keeps_a_bounded_number_of_clients():
    limiter = RateLimiter(clock=Clock(), max_keys=10)
    for number in range(50):
        limiter.hit(f"client-{number}", "all", (5, 60.0))
    assert len(limiter) <= 10


def test_token_attempts_are_limited_even_with_the_right_token(tmp_path):
    with running(tmp_path, limits=RateLimits(token=(2, 300.0))) as server:
        wrong = [exchange(server, "GET", "/?token=wrong", auth=False)[0] for _ in range(2)]
        assert wrong == [403, 403]
        status, headers, body = exchange(
            server, "GET", f"/?token={server.access_token}", auth=False
        )
        assert status == 429
        assert int(headers["Retry-After"]) > 0
        assert "しばらく待って" in body.decode()


def test_posts_are_limited(tmp_path):
    with running(tmp_path, limits=RateLimits(post=(1, 60.0))) as server:
        cookie = login(server)
        body = urlencode({"csrf": "x"})
        assert exchange(server, "POST", "/save", body, {**FORM, **cookie}, auth=False)[0] == 403
        assert exchange(server, "POST", "/save", body, {**FORM, **cookie}, auth=False)[0] == 429
        # 閲覧は POST の制限と別に数える。
        assert exchange(server, "GET", "/", headers=cookie, auth=False)[0] == 200


def test_all_requests_have_an_upper_limit(tmp_path):
    with running(tmp_path, limits=RateLimits(all=(3, 60.0))) as server:
        cookie = login(server)  # トークンの受け渡しで1回
        statuses = [exchange(server, "GET", "/", headers=cookie, auth=False)[0] for _ in range(3)]
        assert statuses == [200, 200, 429]


# --- セッションごとの CSRF トークン ---


def test_csrf_token_belongs_to_one_session(tmp_path):
    """別のセッション（別の人や別の端末）の画面から取ったトークンでは保存できない。"""
    with running(tmp_path) as server:
        mine, theirs = login(server), login(server)
        assert mine != theirs
        token = csrf_token_for(server, mine)
        assert token == csrf_token_for(server, mine)  # 同じセッションなら同じ
        assert token != csrf_token_for(server, theirs)
        body = urlencode({"csrf": token, "action": "missed", "start": "x"})
        # 自分のセッションでは CSRF を通り、ルートが入力の誤りを返す。
        assert exchange(server, "POST", "/save", body, {**FORM, **mine}, auth=False)[0] == 400
        assert exchange(server, "POST", "/save", body, {**FORM, **theirs}, auth=False)[0] == 403


def test_csrf_token_is_not_the_session_id(tmp_path):
    with running(tmp_path) as server:
        cookie = login(server)
        session_id = cookie["Cookie"].split("=", 1)[1]
        page = exchange(server, "GET", "/", headers=cookie, auth=False)[2].decode()
        assert session_id not in page


def csrf_token_for(server, cookie):
    import re

    page = exchange(server, "GET", "/logout", headers=cookie, auth=False)[2].decode()
    return re.search(r'name="csrf" value="([^"]+)"', page)[1]
