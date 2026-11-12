"""役割の変更と監査ログ（#85）を検証する。"""

import re
from urllib.parse import urlencode

import pytest
from support import exchange, login

from poop_detector.errors import InvalidInputError
from poop_detector.gui.accounts import Accounts
from poop_detector.records.database import events_database

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def post(server, cookie, path, fields, page="/account"):
    html = exchange(server, "GET", page, headers=cookie, auth=False)[2].decode()
    token = re.search(r'name="csrf" value="([^"]+)"', html)[1]
    body = urlencode({**fields, "csrf": token})
    return exchange(server, "POST", path, body, {**FORM, **cookie}, auth=False)


# --- 役割の変更 ---


def test_last_active_admin_cannot_become_a_member(tmp_path):
    users = Accounts(events_database(tmp_path)).users
    first = users.create("家族A", "admin")
    member = users.create("家族B", "member")
    with pytest.raises(InvalidInputError, match="管理者"):
        users.set_role(first, "member")
    users.set_role(member, "admin")
    users.set_role(first, "member")
    assert users.get(first).role == "member"
    assert users.get(member).role == "admin"
    with pytest.raises(InvalidInputError, match="役割"):
        users.set_role(member, "owner")
    with pytest.raises(InvalidInputError, match="見つかりません"):
        users.set_role("unknown", "member")


def test_disabled_admins_do_not_count_as_the_remaining_admin(tmp_path):
    users = Accounts(events_database(tmp_path)).users
    first = users.create("家族A", "admin")
    second = users.create("家族B", "admin")
    users.disable(second)
    with pytest.raises(InvalidInputError, match="管理者"):
        users.set_role(first, "member")


def test_admin_changes_roles_from_the_gui(gui):
    server, _, _ = gui
    admin = login(server, "管理者")
    users = server.app.accounts.users
    target = users.create("家族B", "member", email="b@example.org")
    path = "/manage/users"
    fields = {"email": "b@example.org", "action": "role", "role": "admin"}
    assert post(server, admin, path, fields, page=path)[0] == 200
    assert users.get(target).role == "admin"
    page = exchange(server, "GET", path, headers=admin, auth=False)[2].decode()
    assert 'name="role"' in page


def test_gui_refuses_to_demote_the_last_admin(gui):
    server, _, _ = gui
    admin = login(server, "管理者")
    users = server.app.accounts.users
    me = users.by_name("管理者")
    # メールアドレスでしか操作できないので、自分にアドレスをひも付ける。
    with server.app.accounts.users.database.connect() as db:
        db.execute("UPDATE users SET email = 'me@example.org' WHERE id = ?", (me.id,))
    path = "/manage/users"
    fields = {"email": "me@example.org", "action": "role", "role": "member"}
    status, _, body = post(server, admin, path, fields, page=path)
    assert status == 400
    assert "管理者" in body.decode()
    assert users.get(me.id).role == "admin"


# --- 監査ログ ---


def test_audit_log_records_who_did_what_without_secrets(tmp_path):
    accounts = Accounts(events_database(tmp_path))
    admin = accounts.users.create("家族A", "admin")
    accounts.audit.record(admin, "tray.begin", "トレーの交換を開始")
    accounts.audit.record("cli", "user.invite", "b@example.org（一般ユーザー）")
    entries = accounts.audit.recent()
    assert [(entry.actor, entry.action) for entry in entries] == [
        ("監視するマシンのコマンド", "user.invite"),
        ("家族A", "tray.begin"),
    ]
    assert entries[0].detail == "b@example.org（一般ユーザー）"
    assert entries[0].at


def test_admin_operations_appear_in_the_audit_page(gui):
    server, _, _ = gui
    admin = login(server, "管理者A")
    assert post(server, admin, "/manage/tray", {"action": "begin"}, page="/manage")[0] == 303
    users = server.app.accounts.users
    users.create("家族B", "member", email="b@example.org")
    path = "/manage/users"
    post(server, admin, path, {"email": "b@example.org", "action": "disable"}, page=path)
    post(server, admin, "/clips/protect", {"id": "x", "protected": "0"}, page="/clips")
    status, _, body = exchange(server, "GET", "/manage/audit", headers=admin, auth=False)
    page = body.decode()
    assert status == 200
    assert "管理者A" in page
    assert "トレーの交換" in page
    assert "b@example.org" in page
    actions = [entry.action for entry in server.app.accounts.audit.recent()]
    assert actions[:2] == ["user.disable", "tray.begin"]
    assert "csrf" not in page.lower().replace('name="csrf"', "")


def test_invitations_are_audited_without_the_link(gui, monkeypatch):
    from poop_detector.gui import invitation_mail
    from poop_detector.web_address import Site

    server, _, _ = gui
    monkeypatch.setattr(Site, "public_base", property(lambda self: "https://toilet.example.com/"))
    monkeypatch.setattr(invitation_mail, "_send", lambda *_args: None)
    admin = login(server, "管理者")
    fields = {"name": "家族C", "email": "c@example.org", "role": "member"}
    assert post(server, admin, "/manage/invite", fields, page="/manage/invite")[0] == 200
    enrollment = server.app.accounts.enrollments
    entry = server.app.accounts.audit.recent()[0]
    assert (entry.action, entry.actor) == ("user.invite", "管理者")
    assert "c@example.org" in entry.detail
    assert "code" not in entry.detail
    assert enrollment is not None


def test_admin_who_becomes_a_member_returns_to_the_top(gui):
    server, _, _ = gui
    login(server, "別の管理者")
    me = login(server, "管理者")
    users = server.app.accounts.users
    with users.database.connect() as db:
        db.execute("UPDATE users SET email = 'me@example.org' WHERE name = '管理者'")
    path = "/manage/users"
    fields = {"email": "me@example.org", "action": "role", "role": "member"}
    status, headers, _ = post(server, me, path, fields, page=path)
    assert (status, headers["Location"]) == (303, "/")
    assert exchange(server, "GET", path, headers=me, auth=False)[0] == 403
