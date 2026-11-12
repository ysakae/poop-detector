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


def test_invite_requires_a_public_https_url(gui):
    server, _, _ = gui
    cookie = login(server, "家族A")
    status, _, page = exchange(server, "GET", "/manage/invite", headers=cookie, auth=False)
    assert status == 200
    assert 'action="/manage/invite"' in page.decode()
    status, _, page = post(
        server,
        cookie,
        "/manage/invite",
        {"name": "家族C", "email": "family@example.org", "role": "admin"},
        page="/manage/invite",
    )
    assert status == 400
    assert "gui.public_url" in page.decode()


@pytest.mark.parametrize("email", ["", "invalid", "a@example.org\r\nbcc@example.org"])
def test_invite_rejects_invalid_emails(gui, email, monkeypatch):
    from poop_detector.web_address import Site

    server, _, _ = gui
    monkeypatch.setattr(Site, "public_base", property(lambda self: "https://toilet.example.com/"))
    cookie = login(server, "家族A")
    status, _, _ = post(
        server,
        cookie,
        "/manage/invite",
        {"name": "家族C", "email": email, "role": "admin"},
        page="/manage/invite",
    )
    assert status == 400


def test_members_cannot_invite(gui):
    server, _, _ = gui
    cookie = login(server, "家族B", role="member")
    assert exchange(server, "GET", "/manage/invite", headers=cookie, auth=False)[0] == 403
    body = urlencode({"csrf": csrf(server, cookie), "name": "家族C"})
    status, _, _ = exchange(server, "POST", "/manage/invite", body, {**FORM, **cookie}, auth=False)
    assert status == 403


def test_every_page_links_to_my_account(gui):
    server, _, _ = gui
    for path in ("/", "/records", "/clips", "/activity", "/manage", "/account"):
        assert b'href="/account"' in exchange(server, "GET", path)[2]


def test_gui_email_invitation_failure_can_be_retried(gui, monkeypatch):
    from poop_detector.gui import invitation_mail
    from poop_detector.web_address import Site

    server, _, _ = gui
    monkeypatch.setattr(Site, "public_base", property(lambda self: "https://toilet.example.com/"))
    cookie = login(server)
    messages = []

    def send(recipient, subject, body):
        messages.append(body)
        if len(messages) == 1:
            raise invitation_mail.MailError("メールを送信できません")

    monkeypatch.setattr(invitation_mail, "_send", send)
    fields = {"name": "招待される人", "email": "family@example.org", "role": "admin"}
    assert post(server, cookie, "/manage/invite", fields, page="/manage/invite")[0] == 502
    first = re.search(r"enroll\?code=([\w-]+)", messages[0])[1]
    assert server.app.accounts.enrollments.peek(first) is None
    status, _, response = post(
        server,
        cookie,
        "/manage/invite",
        {"email": fields["email"], "action": "resend"},
        page="/manage/invite",
    )
    assert status == 200
    assert b"code=" not in response
    second = re.search(r"enroll\?code=([\w-]+)", messages[1])[1]
    assert server.app.accounts.enrollments.peek(second).email == fields["email"]


def test_existing_user_confirms_address_without_registering_a_passkey(gui, monkeypatch):
    from poop_detector.gui import invitation_mail
    from poop_detector.web_address import Site

    server, _, _ = gui
    monkeypatch.setattr(Site, "public_base", property(lambda self: "https://toilet.example.com/"))
    owner, other = login(server, "所有者"), login(server, "別の人")
    user_id = user_of(server, owner)
    add_key(server, user_id, "old-key", "Mac")
    messages = []
    monkeypatch.setattr(invitation_mail, "_send", lambda _to, _subject, body: messages.append(body))
    assert post(server, owner, "/account/email", {"email": "Owner@example.org"})[0] == 303
    code = re.search(r"confirm\?code=([\w-]+)", messages[0])[1]
    confirm = "/account/email/confirm"
    assert exchange(server, "GET", f"{confirm}?code={code}", headers=owner, auth=False)[0] == 200
    assert server.app.accounts.users.get(user_id).email is None
    assert post(server, other, confirm, {"code": code})[0] == 400
    assert post(server, owner, confirm, {"code": code})[0] == 303
    assert server.app.accounts.users.get(user_id).email == "owner@example.org"
    assert server.app.accounts.passkeys.get("old-key").user_id == user_id


def test_admin_searches_and_disables_by_email(gui):
    server, _, _ = gui
    admin, member = login(server, "管理者"), login(server, "一般", role="member")
    users = server.app.accounts.users
    target = users.create("対象", "member", email="target@example.org")
    users.create("表示しない", "member", email="other@example.org")
    path = "/manage/users"
    assert exchange(server, "GET", path, headers=member, auth=False)[0] == 403
    page = exchange(server, "GET", f"{path}?email=TARGET%40example.org", headers=admin, auth=False)[
        2
    ]
    assert "対象".encode() in page
    assert "表示しない".encode() not in page
    fields = {"email": "target@example.org", "action": "disable"}
    assert post(server, member, path, fields)[0] == 403
    assert post(server, admin, path, fields, page=path)[0] == 200
    assert not users.get(target).active
