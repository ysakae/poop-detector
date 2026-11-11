"""GUI のユーザー・パスキー・登録用 URL の台帳と、users コマンドを検証する。"""

import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from poop_detector.config import CONNECTION_KEYS
from poop_detector.errors import InvalidInputError
from poop_detector.gui.accounts import Accounts, EnrollmentStore, PasskeyStore, UserStore
from poop_detector.main import main
from poop_detector.records.database import events_database


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    accounts = Accounts(events_database(tmp_path), clock=clock)
    return SimpleNamespace(accounts=accounts, clock=clock, path=tmp_path / "events.sqlite3")


# --- ユーザー ---


def test_users_have_unique_names_and_roles(store):
    users: UserStore = store.accounts.users
    admin = users.create("家族A", "admin")
    member = users.create("家族B", "member")
    assert users.get(admin).role == "admin"
    assert users.by_name("家族B").id == member
    assert [user.name for user in users.list()] == ["家族A", "家族B"]
    with pytest.raises(InvalidInputError, match="使われています"):
        users.create("家族A", "member")
    with pytest.raises(InvalidInputError, match="役割"):
        users.create("家族C", "owner")
    with pytest.raises(InvalidInputError, match="名前"):
        users.create("  ", "member")


def test_last_active_admin_cannot_be_disabled(store):
    users = store.accounts.users
    first = users.create("家族A", "admin")
    member = users.create("家族B", "member")
    with pytest.raises(InvalidInputError, match="管理者"):
        users.disable(first)
    users.disable(member)
    assert not users.get(member).active
    second = users.create("家族C", "admin")
    users.disable(first)
    assert not users.get(first).active
    assert users.get(second).active
    users.enable(first)
    assert users.get(first).active


# --- パスキー ---


def test_passkeys_belong_to_a_user_and_a_domain(store):
    accounts = store.accounts
    user = accounts.users.create("家族A", "admin")
    keys: PasskeyStore = accounts.passkeys
    keys.add(user, "localhost", "cred-1", b"public-key", 0, ["internal"], "Mac")
    keys.add(user, "toilet.example.com", "cred-2", b"other-key", 5, [], "スマートフォン")
    assert [key.id for key in keys.for_user(user)] == ["cred-1", "cred-2"]
    key = keys.get("cred-2")
    assert (key.user_id, key.rp_id, key.public_key, key.sign_count) == (
        user,
        "toilet.example.com",
        b"other-key",
        5,
    )
    assert key.transports == []
    assert keys.get("cred-1").transports == ["internal"]
    keys.used("cred-2", 6)
    assert keys.get("cred-2").sign_count == 6
    assert keys.get("cred-2").last_used is not None
    keys.delete("cred-1")
    assert keys.get("cred-1") is None


# --- 登録用 URL ---


def test_enrollment_code_is_single_use_and_only_its_hash_is_stored(store):
    enrollments: EnrollmentStore = store.accounts.enrollments
    code = enrollments.issue("admin", name="家族A", created_by="cli")
    assert len(code) >= 40
    with sqlite3.connect(store.path) as db:
        assert code not in {row[0] for row in db.execute("SELECT id FROM enrollments")}
    pending = enrollments.peek(code)
    assert (pending.role, pending.name, pending.user_id) == ("admin", "家族A", None)
    assert enrollments.consume(code).role == "admin"
    assert enrollments.peek(code) is None
    assert enrollments.consume(code) is None
    assert enrollments.peek("unknown") is None


def test_enrollment_code_expires(store):
    enrollments = store.accounts.enrollments
    code = enrollments.issue("member", name="家族B", created_by="cli", minutes=30)
    store.clock.now += 29 * 60
    assert enrollments.peek(code) is not None
    store.clock.now += 2 * 60
    assert enrollments.peek(code) is None
    assert enrollments.consume(code) is None


def test_recovery_enrollment_adds_a_passkey_to_an_existing_user(store):
    accounts = store.accounts
    user = accounts.users.create("家族A", "admin")
    code = accounts.enrollments.issue_recovery(user, created_by="cli")
    pending = accounts.enrollments.peek(code)
    assert (pending.user_id, pending.role, pending.name) == (user, "admin", "家族A")


# --- users コマンド ---


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in CONNECTION_KEYS:
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "data"
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {root}\n")

    def run(*args):
        return main(["--config", str(config), "users", *args])

    return SimpleNamespace(run=run, root=root, config=config)


def enrollment_code(out):
    url = next(line for line in out.splitlines() if "/enroll?" in line).split()[-1]
    return url, parse_qs(urlsplit(url).query)["code"][0]


def test_first_user_must_be_an_admin(cli, capsys):
    assert cli.run("invite", "家族A") == 2
    assert "--role admin" in capsys.readouterr().err
    assert cli.run("invite", "家族A", "--role", "admin") == 0
    url, code = enrollment_code(capsys.readouterr().out)
    assert url.startswith("http://localhost:8765/enroll?")
    accounts = Accounts(events_database(cli.root))
    assert accounts.enrollments.peek(code).role == "admin"


def test_invite_uses_the_public_url_when_set(cli, capsys):
    cli.config.write_text(
        f"data_dir: {cli.root}\ngui: {{public_url: 'https://toilet.example.com'}}\n"
    )
    assert cli.run("invite", "家族A", "--role", "admin", "--minutes", "10") == 0
    url, _ = enrollment_code(capsys.readouterr().out)
    assert url.startswith("https://toilet.example.com/enroll?code=")


def test_users_list_recover_disable_and_enable(cli, capsys):
    accounts = Accounts(events_database(cli.root))
    admin = accounts.users.create("家族A", "admin")
    accounts.users.create("家族B", "member")
    accounts.passkeys.add(admin, "localhost", "cred-1", b"k", 0, [], "Mac")

    assert cli.run("list") == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(user["name"], user["role"], user["passkeys"]) for user in listed] == [
        ("家族A", "admin", 1),
        ("家族B", "member", 0),
    ]
    assert all("id" not in user or user["id"] for user in listed)

    assert cli.run("recover", "家族A") == 0
    _, code = enrollment_code(capsys.readouterr().out)
    assert accounts.enrollments.peek(code).user_id == admin

    assert cli.run("disable", "家族B") == 0
    assert not accounts.users.by_name("家族B").active
    assert cli.run("enable", "家族B") == 0
    assert accounts.users.by_name("家族B").active
    assert cli.run("disable", "家族A") == 2
    assert "管理者" in capsys.readouterr().err
    assert cli.run("recover", "いない人") == 2
    assert cli.run("invite") == 2


def test_members_cannot_be_invited_until_roles_are_enforced(cli, capsys):
    """権限の確認（#85）が入るまでは、一般ユーザーも管理者と同じ操作ができてしまう。

    そのため、一般ユーザーの招待は断り、管理者だけを招待できるようにしておく。
    """
    assert cli.run("invite", "家族A", "--role", "admin") == 0
    capsys.readouterr()
    assert cli.run("invite", "家族B") == 2
    assert "#85" in capsys.readouterr().err
    assert cli.run("invite", "家族B", "--role", "member") == 2
    accounts = Accounts(events_database(cli.root))
    assert accounts.users.by_name("家族B") is None


def test_mail_invite_reissue_cancel_and_duplicate(store):
    accounts = store.accounts
    invites = accounts.enrollments
    first = invites.issue_email(
        " Family@Example.org ", name="家族A", role="admin", created_by="cli"
    )
    assert invites.peek(first).email == "family@example.org"
    with pytest.raises(InvalidInputError, match="再送"):
        invites.issue_email("family@example.org", name="別名", role="admin", created_by="cli")
    second = invites.issue_email("family@example.org", created_by="cli", resend=True)
    assert invites.peek(first) is None
    assert invites.peek(second).name == "家族A"
    assert invites.cancel_email("FAMILY@example.org")
    assert invites.peek(second) is None
    accounts.users.create("登録済み", "admin", email="family@example.org")
    with pytest.raises(InvalidInputError, match="登録"):
        invites.issue_email("family@example.org", created_by="cli", resend=True)


def test_verified_email_is_bound_to_signed_in_user(store):
    accounts = store.accounts
    owner = accounts.users.create("所有者", "admin")
    other = accounts.users.create("別の人", "admin")
    code = accounts.enrollments.issue_email("Owner@example.org", created_by=owner, user_id=owner)
    assert accounts.enrollments.peek(code) is None
    assert not accounts.enrollments.verify_email(code, other)
    assert accounts.enrollments.verify_email(code, owner)
    assert not accounts.enrollments.verify_email(code, owner)
    assert accounts.users.by_email("OWNER@example.org").id == owner
    assert accounts.users.get(other).email is None


def test_cli_invites_by_email_without_printing_link(cli, capsys, monkeypatch):
    from poop_detector.gui import invitation_mail

    messages = []
    monkeypatch.setattr(
        invitation_mail,
        "_send",
        lambda recipient, subject, body: messages.append((recipient, subject, body)),
    )
    cli.config.write_text(
        f"data_dir: {cli.root}\ngui: {{public_url: 'https://toilet.example.com'}}\n"
    )
    assert cli.run("invite", "Family@example.org", "--name", "家族", "--role", "admin") == 0
    assert "code=" not in capsys.readouterr().out
    assert messages[0][0] == "family@example.org"
    assert "https://toilet.example.com/enroll?code=" in messages[0][2]
    assert cli.run("cancel-invite", "family@example.org") == 0


def test_only_one_concurrent_invitation_per_address(store):
    from concurrent.futures import ThreadPoolExecutor

    def invite(name):
        try:
            return store.accounts.enrollments.issue_email(
                "family@example.org", name=name, created_by="cli"
            )
        except InvalidInputError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invite, ["家族A", "家族B"]))
    assert sum(code is not None for code in results) == 1


def test_v5_database_preserves_passkeys_and_unused_enrollment(tmp_path):
    from poop_detector.gui.accounts import _digest
    from poop_detector.records.database import MIGRATIONS

    with sqlite3.connect(tmp_path / "events.sqlite3") as db:
        for migration in MIGRATIONS[:5]:
            migration(db)
        db.execute("PRAGMA user_version = 5")
        db.execute(
            "INSERT INTO users (id, name, role, created_at) VALUES ('old', '家族A', 'admin', '2026')"
        )
        db.execute(
            "INSERT INTO passkeys (id, user_id, rp_id, public_key, sign_count, transports, label, "
            "created_at) VALUES ('key', 'old', 'localhost', X'AB', 3, '[]', 'Mac', '2026')"
        )
        db.execute(
            "INSERT INTO enrollments (id, role, name, user_id, created_by, created_at, expires_at) "
            "VALUES (?, 'admin', '家族A', 'old', 'cli', '2026', '2999')",
            (_digest("legacy-code"),),
        )
    accounts = Accounts(events_database(tmp_path))
    assert accounts.users.get("old").email is None
    assert accounts.passkeys.get("key").sign_count == 3
    assert accounts.enrollments.peek("legacy-code").user_id == "old"
    code = accounts.enrollments.issue_email("owner@example.org", created_by="old", user_id="old")
    assert accounts.enrollments.verify_email(code, "old")
    assert accounts.users.by_email("owner@example.org").id == "old"
    assert accounts.passkeys.get("key").user_id == "old"
