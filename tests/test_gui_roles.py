"""管理者と一般ユーザーの権限（#85）を、ルートの表のすべてのルートについて検証する。

ルートの表から一覧を作るので、ルートを足すと自動で対象になる。管理者だけのルートを足すときは、
下の ADMIN_GET・ADMIN_POST にも加える（表と宣言がずれるとテストが失敗する）。
"""

import re
from urllib.parse import urlencode

import pytest
from support import exchange, login

from poop_detector.gui.asgi import is_public
from poop_detector.gui.routes import GET_ROUTES, POST_ROUTES, required_role

FORM = {"Content-Type": "application/x-www-form-urlencoded"}

# 権限の表（Issue #85）で、管理者だけができる操作の入口。
ADMIN_GET = {
    "/manage",
    "/manage/status",
    "/manage/storage",
    "/manage/invite",
    "/manage/users",
    "/manage/audit",
    "/manage/settings",
    "/manage/checks",
    "/manage/models",
    "/manage/areas",
    "/manage/areas/frame",
}
ADMIN_POST = {
    "/manage/tray",
    "/manage/storage/cleanup",
    "/manage/invite",
    "/manage/users",
    "/manage/settings",
    "/manage/checks",
    "/manage/models",
    "/manage/areas",
}
MEMBER_GET = sorted(path for path in GET_ROUTES if not is_public(path) and path not in ADMIN_GET)
MEMBER_POST = sorted(path for path in POST_ROUTES if path not in ADMIN_POST)


def csrf_of(server, cookie):
    page = exchange(server, "GET", "/account", headers=cookie, auth=False)[2].decode()
    return re.search(r'name="csrf" value="([^"]+)"', page)[1]


def post(server, cookie, path, fields):
    body = urlencode({**fields, "csrf": csrf_of(server, cookie)})
    return exchange(server, "POST", path, body, {**FORM, **cookie}, auth=False)


def test_route_table_declares_the_roles_of_the_permission_table():
    declared_get = {path for path in GET_ROUTES if required_role("GET", path) == "admin"}
    declared_post = {path for path in POST_ROUTES if required_role("POST", path) == "admin"}
    assert declared_get == ADMIN_GET
    assert declared_post == ADMIN_POST


@pytest.mark.parametrize("path", sorted(ADMIN_GET))
def test_members_are_refused_admin_pages(gui, path):
    server, _, _ = gui
    member = login(server, role="member")
    status, _, body = exchange(server, "GET", path, headers=member, auth=False)
    assert status == 403
    assert "管理者だけの操作です" in body.decode()
    assert 'href="/"' in body.decode()
    # 管理者は断られない（カメラのない環境では、映像の取得が 503 になる画面もある）。
    assert exchange(server, "GET", path, headers=login(server), auth=False)[0] != 403


@pytest.mark.parametrize("path", sorted(ADMIN_POST))
def test_members_are_refused_admin_operations(gui, path):
    server, _, _ = gui
    member = login(server, role="member")
    status, _, _ = post(server, member, path, {"action": "begin", "confirm": "1"})
    assert status == 403
    status, _, _ = post(server, login(server), path, {"action": "unknown"})
    assert status != 403


@pytest.mark.parametrize("path", MEMBER_GET)
def test_members_can_open_the_other_pages(gui, path):
    server, _, _ = gui
    status, _, _ = exchange(server, "GET", path, headers=login(server, role="member"), auth=False)
    assert status != 403


@pytest.mark.parametrize("path", MEMBER_POST)
def test_members_can_use_the_other_operations(gui, path):
    server, _, _ = gui
    status, _, _ = post(server, login(server, role="member"), path, {"id": "x"})
    assert status != 403


def test_members_can_protect_but_not_unprotect_clips(gui):
    server, _, _ = gui
    member = login(server, role="member")
    assert post(server, member, "/clips/protect", {"id": "x", "protected": "1"})[0] != 403
    assert post(server, member, "/clips/protect", {"id": "x", "protected": "0"})[0] == 403
    assert post(server, login(server), "/clips/protect", {"id": "x", "protected": "0"})[0] != 403


@pytest.mark.parametrize(
    ("path", "admin_only"),
    [
        ("/", ['href="/manage"']),
        ("/clips", ['href="/manage/storage"']),
        ("/account", ['href="/manage"']),
    ],
)
def test_admin_entry_points_are_hidden_from_members(gui, path, admin_only):
    server, _, _ = gui
    member = exchange(server, "GET", path, headers=login(server, role="member"), auth=False)[2]
    admin = exchange(server, "GET", path, headers=login(server), auth=False)[2]
    for link in admin_only:
        assert link.encode() not in member
        assert link.encode() in admin


def test_role_changes_apply_to_the_next_request(gui):
    server, _, _ = gui
    login(server, "残る管理者")
    cookie = login(server, "家族A")
    assert exchange(server, "GET", "/manage", headers=cookie, auth=False)[0] == 200
    users = server.app.accounts.users
    users.set_role(users.by_name("家族A").id, "member")
    assert exchange(server, "GET", "/manage", headers=cookie, auth=False)[0] == 403


def test_management_menu_only_lists_admin_pages():
    from poop_detector.gui.navigation import MANAGEMENT_PAGES

    assert {href for href, _ in MANAGEMENT_PAGES} <= ADMIN_GET
