"""GUI のユーザー、パスキー、登録用 URL の台帳。

- users：表示名（重複しない）と役割（admin・member）。管理者は最低1人残す
- passkeys：WebAuthn の公開鍵。登録したドメイン（RP ID）ごとに持つ
- enrollments：パスキーを登録するための使い捨ての URL。値そのものは保存せず SHA-256 だけを持つ

時刻は UTC の ISO 8601 で保存する。clock はテストで差し替える（エポック秒を返す）。
"""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeAlias
from uuid import uuid4

from ..errors import InvalidInputError
from ..records.database import Database

Role: TypeAlias = Literal["admin", "member"]
ROLES: dict[Role, str] = {"admin": "管理者", "member": "一般ユーザー"}
NAME_MAX_LENGTH = 40
ENROLLMENT_MINUTES = 30
Clock: TypeAlias = Callable[[], float]


def _stamp(seconds: float) -> str:
    """文字列のままで前後を比べられるよう、桁をそろえた UTC の ISO 8601。"""
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="microseconds")


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _role(value: str) -> Role:
    if value not in ROLES:
        raise InvalidInputError("役割は admin か member を指定してください")
    return value


def normalize_email(value: str) -> str:
    """メールアドレスを照合用の小文字にそろえ、SMTP で扱える形式を確認する。"""
    if any(char in value for char in "\r\n"):
        raise InvalidInputError("メールアドレスの形式を確認してください")
    email = value.strip().lower()
    local, separator, domain = email.partition("@")
    atoms = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
    domain_label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if (
        not separator
        or len(email) > 254
        or len(local) > 64
        or not re.fullmatch(rf"{atoms}(?:\.{atoms})*", local, re.ASCII)
        or not re.fullmatch(rf"{domain_label}(?:\.{domain_label})+", domain, re.ASCII)
    ):
        raise InvalidInputError("メールアドレスの形式を確認してください")
    return email


def checked_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > NAME_MAX_LENGTH:
        raise InvalidInputError(f"名前は1〜{NAME_MAX_LENGTH}文字で指定してください")
    return name


@dataclass(frozen=True)
class User:
    """GUI を使う人。"""

    id: str
    name: str
    role: Role
    created_at: str
    last_login: str | None
    disabled_at: str | None
    email: str | None = None

    @property
    def active(self) -> bool:
        return self.disabled_at is None


@dataclass(frozen=True)
class Passkey:
    """ユーザーが登録したパスキー（WebAuthn の資格情報）。"""

    id: str
    user_id: str
    rp_id: str
    public_key: bytes
    sign_count: int
    transports: list[str]
    label: str
    created_at: str
    last_used: str | None


@dataclass(frozen=True)
class Enrollment:
    """パスキーを登録するための URL の中身。user_id があれば、そのユーザーへの追加（復旧）。"""

    role: Role
    name: str | None
    user_id: str | None
    created_by: str
    expires_at: str
    email: str | None = None


class UserStore:
    """users テーブル。"""

    def __init__(self, database: Database, clock: Clock) -> None:
        self.database = database
        self.clock = clock

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(
            row["id"],
            row["name"],
            _role(row["role"]),
            row["created_at"],
            row["last_login"],
            row["disabled_at"],
            row["email"],
        )

    def create(
        self,
        name: str,
        role: str,
        *,
        user_id: str | None = None,
        email: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        """ユーザーを作る。パスキー登録中は同じトランザクションを使う。"""
        name = checked_name(name)
        checked = _role(role)
        email = normalize_email(email) if email is not None else None
        user_id = user_id or uuid4().hex
        try:
            with (
                nullcontext(connection) if connection is not None else self.database.connect() as db
            ):
                db.execute(
                    "INSERT INTO users (id, name, role, created_at, email) VALUES (?, ?, ?, ?, ?)",
                    (user_id, name, checked, _stamp(self.clock()), email),
                )
        except sqlite3.IntegrityError as error:
            if "users.email" in str(error):
                raise InvalidInputError("このメールアドレスはすでに登録されています") from None
            raise InvalidInputError(f"「{name}」という名前はすでに使われています") from None
        return user_id

    def by_email(self, email: str) -> User | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM users WHERE email = ?", (normalize_email(email),)
            ).fetchone()
        return self._user(row) if row else None

    def get(self, user_id: str) -> User | None:
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._user(row) if row else None

    def by_name(self, name: str) -> User | None:
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM users WHERE name = ?", (name.strip(),)).fetchone()
        return self._user(row) if row else None

    def list(self) -> list[User]:
        with self.database.connect() as db:
            rows = db.execute("SELECT * FROM users ORDER BY created_at, name").fetchall()
        return [self._user(row) for row in rows]

    def disable(self, user_id: str) -> None:
        """ユーザーを無効にする。最後の有効な管理者は無効にできない。"""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT role, disabled_at FROM users WHERE id = ?", (user_id,))
            user = row.fetchone()
            if user is None:
                raise InvalidInputError("ユーザーが見つかりません")
            others = db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled_at IS NULL "
                "AND id != ?",
                (user_id,),
            ).fetchone()[0]
            if user["role"] == "admin" and user["disabled_at"] is None and others == 0:
                raise InvalidInputError(
                    "最後の管理者は無効にできません。先に別の管理者を登録してください"
                )
            db.execute(
                "UPDATE users SET disabled_at = ? WHERE id = ? AND disabled_at IS NULL",
                (_stamp(self.clock()), user_id),
            )

    def enable(self, user_id: str) -> None:
        with self.database.connect() as db:
            db.execute("UPDATE users SET disabled_at = NULL WHERE id = ?", (user_id,))

    def logged_in(self, user_id: str) -> None:
        with self.database.connect() as db:
            db.execute(
                "UPDATE users SET last_login = ? WHERE id = ?", (_stamp(self.clock()), user_id)
            )


class PasskeyStore:
    """passkeys テーブル。"""

    def __init__(self, database: Database, clock: Clock) -> None:
        self.database = database
        self.clock = clock

    @staticmethod
    def _passkey(row: sqlite3.Row) -> Passkey:
        return Passkey(
            row["id"],
            row["user_id"],
            row["rp_id"],
            bytes(row["public_key"]),
            row["sign_count"],
            json.loads(row["transports"]),
            row["label"],
            row["created_at"],
            row["last_used"],
        )

    def add(
        self,
        user_id: str,
        rp_id: str,
        credential_id: str,
        public_key: bytes,
        sign_count: int,
        transports: list[str],
        label: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        with nullcontext(connection) if connection is not None else self.database.connect() as db:
            db.execute(
                "INSERT INTO passkeys (id, user_id, rp_id, public_key, sign_count, transports, "
                "label, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    credential_id,
                    user_id,
                    rp_id,
                    public_key,
                    sign_count,
                    json.dumps(transports),
                    label,
                    _stamp(self.clock()),
                ),
            )

    def get(self, credential_id: str) -> Passkey | None:
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM passkeys WHERE id = ?", (credential_id,)).fetchone()
        return self._passkey(row) if row else None

    def for_user(self, user_id: str) -> list[Passkey]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM passkeys WHERE user_id = ? ORDER BY created_at", (user_id,)
            ).fetchall()
        return [self._passkey(row) for row in rows]

    def counts(self) -> dict[str, int]:
        """ユーザーごとのパスキーの数。"""
        with self.database.connect() as db:
            rows = db.execute("SELECT user_id, COUNT(*) FROM passkeys GROUP BY user_id")
            return {row[0]: row[1] for row in rows}

    def used(self, credential_id: str, sign_count: int) -> None:
        """ログインに使った記録と、認証器の署名の回数を更新する。"""
        with self.database.connect() as db:
            db.execute(
                "UPDATE passkeys SET sign_count = ?, last_used = ? WHERE id = ?",
                (sign_count, _stamp(self.clock()), credential_id),
            )

    def delete(self, credential_id: str) -> None:
        with self.database.connect() as db:
            db.execute("DELETE FROM passkeys WHERE id = ?", (credential_id,))


class EnrollmentStore:
    """enrollments テーブル。登録用 URL の値は返すだけで、保存はハッシュだけ。"""

    def __init__(self, database: Database, users: UserStore, clock: Clock) -> None:
        self.database = database
        self.users = users
        self.clock = clock

    def issue(
        self,
        role: str,
        *,
        name: str | None,
        created_by: str,
        minutes: float = ENROLLMENT_MINUTES,
        user_id: str | None = None,
    ) -> str:
        """新しいユーザー（user_id があれば既存のユーザー）の登録用 URL の値を作る。"""
        code = secrets.token_urlsafe(32)
        now = self.clock()
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO enrollments (id, role, name, user_id, created_by, created_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _digest(code),
                    _role(role),
                    name.strip() if name else None,
                    user_id,
                    created_by,
                    _stamp(now),
                    _stamp(now + minutes * 60),
                ),
            )
        return code

    def issue_email(
        self,
        email: str,
        *,
        created_by: str,
        name: str = "",
        role: str = "admin",
        minutes: float = ENROLLMENT_MINUTES,
        resend: bool = False,
        user_id: str | None = None,
    ) -> str:
        """メール招待または既存ユーザー向けのアドレス確認を発行する。"""
        email = normalize_email(email)
        code = secrets.token_urlsafe(32)
        now = self.clock()
        stamp = _stamp(now)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                raise InvalidInputError("このメールアドレスはすでに登録されています")
            if user_id is not None:
                user = db.execute(
                    "SELECT name, role FROM users WHERE id = ? AND disabled_at IS NULL "
                    "AND email IS NULL",
                    (user_id,),
                ).fetchone()
                if user is None:
                    raise InvalidInputError("このアカウントにはメールアドレスをひも付けられません")
                name, role = user["name"], user["role"]
                db.execute(
                    "UPDATE enrollments SET revoked_at = ? WHERE user_id = ? AND email IS NOT NULL "
                    "AND used_at IS NULL AND revoked_at IS NULL",
                    (stamp, user_id),
                )
            elif resend:
                previous = db.execute(
                    "SELECT name, role FROM enrollments WHERE email = ? AND user_id IS NULL "
                    "AND used_at IS NULL ORDER BY rowid DESC LIMIT 1",
                    (email,),
                ).fetchone()
                if previous is None:
                    raise InvalidInputError("再送できる招待が見つかりません")
                name, role = previous["name"], previous["role"]
                db.execute(
                    "UPDATE enrollments SET revoked_at = ? WHERE email = ? AND user_id IS NULL "
                    "AND used_at IS NULL AND revoked_at IS NULL",
                    (stamp, email),
                )
            if db.execute(
                "SELECT 1 FROM enrollments WHERE email = ? AND used_at IS NULL "
                "AND revoked_at IS NULL AND expires_at > ?",
                (email, stamp),
            ).fetchone():
                raise InvalidInputError(
                    "この宛先には有効な招待または確認があります。招待は再送を使ってください"
                )
            if user_id is None and role != "admin":
                raise InvalidInputError("一般ユーザーの招待は権限制御（#85）の実装後に使えます")
            name = checked_name(name)
            if (
                user_id is None
                and db.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone()
            ):
                raise InvalidInputError("この表示名はすでに使われています")
            db.execute(
                "INSERT INTO enrollments (id, role, name, user_id, created_by, created_at, "
                "expires_at, email) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _digest(code),
                    _role(role),
                    name,
                    user_id,
                    created_by,
                    stamp,
                    _stamp(now + minutes * 60),
                    email,
                ),
            )
        return code

    def cancel_email(self, email: str) -> bool:
        """この宛先の未使用の招待を取り消す。"""
        now = _stamp(self.clock())
        with self.database.connect() as db:
            changed = db.execute(
                "UPDATE enrollments SET revoked_at = ? WHERE email = ? AND user_id IS NULL "
                "AND used_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
                (now, normalize_email(email), now),
            ).rowcount
        return bool(changed)

    def revoke(self, code: str) -> None:
        """メール送信に失敗したリンクだけを失効させる。"""
        with self.database.connect() as db:
            db.execute(
                "UPDATE enrollments SET revoked_at = ? WHERE id = ? AND used_at IS NULL "
                "AND revoked_at IS NULL",
                (_stamp(self.clock()), _digest(code)),
            )

    def verify_email(self, code: str, user_id: str) -> bool:
        """メールを受信し、対象のアカウントにログインできる人だけがひも付けられる。"""
        try:
            with self.database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = self._valid(db, code)
                if row is None or row["email"] is None or row["user_id"] != user_id:
                    return False
                updated = db.execute(
                    "UPDATE users SET email = ? WHERE id = ? AND email IS NULL AND disabled_at IS NULL",
                    (row["email"], user_id),
                ).rowcount
                if updated:
                    self._mark_used(db, row)
                return bool(updated)
        except sqlite3.IntegrityError:
            return False

    def issue_recovery(
        self, user_id: str, *, created_by: str, minutes: float = ENROLLMENT_MINUTES
    ) -> str:
        """既存のユーザーにパスキーを足す（なくしたときの復旧）ための値を作る。"""
        user = self.users.get(user_id)
        if user is None:
            raise InvalidInputError("ユーザーが見つかりません")
        return self.issue(
            user.role, name=user.name, created_by=created_by, minutes=minutes, user_id=user.id
        )

    def _valid(self, db: sqlite3.Connection, code: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = db.execute(
            "SELECT * FROM enrollments WHERE id = ? AND used_at IS NULL "
            "AND revoked_at IS NULL AND expires_at > ?",
            (_digest(code), _stamp(self.clock())),
        ).fetchone()
        return row

    @staticmethod
    def _enrollment(row: sqlite3.Row) -> Enrollment:
        return Enrollment(
            _role(row["role"]),
            row["name"],
            row["user_id"],
            row["created_by"],
            row["expires_at"],
            row["email"],
        )

    def peek(self, code: str) -> Enrollment | None:
        """使える値なら中身を返す（使ったことにはしない）。"""
        with self.database.connect() as db:
            row = self._valid(db, code)
        return self._enrollment(row) if row is not None and self._for_registration(row) else None

    @staticmethod
    def _for_registration(row: sqlite3.Row) -> bool:
        return row["email"] is None or row["user_id"] is None

    def _mark_used(self, db: sqlite3.Connection, row: sqlite3.Row) -> None:
        db.execute(
            "UPDATE enrollments SET used_at = ? WHERE id = ?", (_stamp(self.clock()), row["id"])
        )

    @contextmanager
    def registration(self, code: str) -> Iterator[tuple[sqlite3.Connection, Enrollment | None]]:
        """リンクの使用とパスキーの登録を一緒に確定する。"""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._valid(db, code)
            if row is None or not self._for_registration(row):
                yield db, None
            else:
                yield db, self._enrollment(row)
                self._mark_used(db, row)

    def consume(self, code: str) -> Enrollment | None:
        """値を使ったことにして中身を返す。"""
        with self.registration(code) as (_, enrollment):
            return enrollment


class Accounts:
    """ユーザー・パスキー・登録用 URL の台帳をまとめる。"""

    def __init__(self, database: Database, *, clock: Clock = time.time) -> None:
        self.users = UserStore(database, clock)
        self.passkeys = PasskeyStore(database, clock)
        self.enrollments = EnrollmentStore(database, self.users, clock)

    def expires_in(self, enrollment: Enrollment) -> timedelta:
        """登録用 URL が切れるまでの時間。"""
        expires = datetime.fromisoformat(enrollment.expires_at)
        return expires - datetime.fromtimestamp(self.users.clock(), UTC)
