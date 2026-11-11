"""自分のアカウントの画面（パスキーの一覧・追加・削除）と、画面からの招待を検証する。"""

import re
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from support import exchange, login

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def csrf(server, cookie, page="/account"):
    body = exchange(server, "GET", page, headers=cookie, auth=False)[2].decode()
    return re.search(r'name="csrf" value="([^"]+)"', body)[1]


def post(server, cookie, path, fields, page="/account"):
    body = urlencode({**fields, "csrf": csrf(server, cookie, page)})
    return exchange(server, "POST", path, body, {**FORM, **cookie}, auth=False)


def user_of(server, cookie):
    session = cookie["Cookie"].split("=", 1)[1]
    return server.app.sessions.lookup(session).user_id


def add_key(server, user_id, key_id, label, rp_id="localhost"):
    server.app.accounts.passkeys.add(user_id, rp_id, key_id, b"public", 0, [], label)


def test_account_page_lists_only_my_passkeys(gui):
    server, _, _ = gui
    mine, theirs = login(server, "家族A"), login(server, "家族B")
    add_key(server, user_of(server, mine), "key-1", "<b>Mac</b>")
    add_key(server, user_of(server, theirs), "key-2", "他の人の端末")
    status, _, page = exchange(server, "GET", "/account", headers=mine, auth=False)
    text = page.decode()
    assert status == 200
    assert "家族A" in text
    assert "&lt;b&gt;Mac&lt;/b&gt;" in text
    assert "localhost" in text
    assert "他の人の端末" not in text


def test_last_passkey_cannot_be_deleted(gui):
    server, _, _ = gui
    cookie = login(server)
    user = user_of(server, cookie)
    add_key(server, user, "key-1", "Mac")
    status, _, page = post(server, cookie, "/account/passkeys/delete", {"id": "key-1"})
    assert status == 400
    assert "最後" in page.decode()
    assert server.app.accounts.passkeys.get("key-1") is not None

    add_key(server, user, "key-2", "スマートフォン")
    status, headers, _ = post(server, cookie, "/account/passkeys/delete", {"id": "key-1"})
    assert status == 303
    assert headers["Location"] == "/account?deleted=1"
    assert server.app.accounts.passkeys.get("key-1") is None


def test_passkeys_of_other_users_cannot_be_deleted(gui):
    server, _, _ = gui
    mine, theirs = login(server, "家族A"), login(server, "家族B")
    add_key(server, user_of(server, mine), "key-1", "Mac")
    for key in ("key-2", "key-3"):
        add_key(server, user_of(server, theirs), key, "他の人の端末")
    status, _, _ = post(server, mine, "/account/passkeys/delete", {"id": "key-2"})
    assert status == 404
    assert server.app.accounts.passkeys.get("key-2") is not None


def test_adding_a_passkey_goes_through_a_short_lived_enrollment_for_myself(gui):
    server, _, _ = gui
    cookie = login(server, "家族A")
    status, headers, _ = post(server, cookie, "/account/passkeys/add", {})
    assert status == 303
    location = urlsplit(headers["Location"])
    assert location.path == "/enroll"
    code = parse_qs(location.query)["code"][0]
    enrollment = server.app.accounts.enrollments.peek(code)
    assert enrollment.user_id == user_of(server, cookie)
    assert enrollment.created_by == user_of(server, cookie)


def test_admin_can_invite_from_the_gui(gui):
    server, _, _ = gui
    cookie = login(server, "家族A")
    status, _, page = exchange(server, "GET", "/account/invite", headers=cookie, auth=False)
    assert status == 200
    assert 'action="/account/invite"' in page.decode()
    status, _, page = post(
        server, cookie, "/account/invite", {"name": "家族C"}, page="/account/invite"
    )
    text = page.decode()
    assert status == 200
    url = re.search(r"(http://localhost:\d+/enroll\?code=[\w-]+)", text)[1]
    code = parse_qs(urlsplit(url).query)["code"][0]
    enrollment = server.app.accounts.enrollments.peek(code)
    assert (enrollment.name, enrollment.role) == ("家族C", "admin")
    assert enrollment.created_by == user_of(server, cookie)


@pytest.mark.parametrize("name", ["家族A", "", "x" * 41])
def test_invite_rejects_taken_or_invalid_names(gui, name):
    server, _, _ = gui
    cookie = login(server, "家族A")
    status, _, _ = post(server, cookie, "/account/invite", {"name": name}, page="/account/invite")
    assert status == 400


def test_members_cannot_invite(gui):
    server, _, _ = gui
    cookie = login(server, "家族B", role="member")
    assert exchange(server, "GET", "/account/invite", headers=cookie, auth=False)[0] == 403
    body = urlencode({"csrf": csrf(server, cookie), "name": "家族C"})
    status, _, _ = exchange(server, "POST", "/account/invite", body, {**FORM, **cookie}, auth=False)
    assert status == 403


def test_every_page_links_to_my_account(gui):
    server, _, _ = gui
    for path in ("/", "/records", "/clips", "/activity", "/manage", "/account"):
        assert b'href="/account"' in exchange(server, "GET", path)[2]
