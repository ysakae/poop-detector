"""GUI のユーザー、パスキー、登録用 URL の台帳。

- users：表示名（重複しない）と役割（admin・member）。管理者は最低1人残す
- passkeys：WebAuthn の公開鍵。登録したドメイン（RP ID）ごとに持つ
- enrollments：パスキーを登録するための使い捨ての URL。値そのものは保存せず SHA-256 だけを持つ

時刻は UTC の ISO 8601 で保存する。clock はテストで差し替える（エポック秒を返す）。
"""

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
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


@dataclass(frozen=True)
class User:
    """GUI を使う人。"""

    id: str
    name: str
    role: Role
    created_at: str
    last_login: str | None
    disabled_at: str | None

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
        )

    def create(self, name: str, role: str, *, user_id: str | None = None) -> str:
        """ユーザーを作り、ID を返す。user_id はパスキーの登録で決めた ID を使うとき。"""
        name = name.strip()
        if not name or len(name) > NAME_MAX_LENGTH:
            raise InvalidInputError(f"名前は1〜{NAME_MAX_LENGTH}文字で指定してください")
        checked = _role(role)
        user_id = user_id or uuid4().hex
        try:
            with self.database.connect() as db:
                db.execute(
                    "INSERT INTO users (id, name, role, created_at) VALUES (?, ?, ?, ?)",
                    (user_id, name, checked, _stamp(self.clock())),
                )
        except sqlite3.IntegrityError:
            raise InvalidInputError(f"「{name}」という名前はすでに使われています") from None
        return user_id

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

    def active_admins(self) -> int:
        with self.database.connect() as db:
            count: int = db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled_at IS NULL"
            ).fetchone()[0]
        return count

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
    ) -> None:
        with self.database.connect() as db:
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
            "SELECT * FROM enrollments WHERE id = ? AND used_at IS NULL AND expires_at > ?",
            (_digest(code), _stamp(self.clock())),
        ).fetchone()
        return row

    @staticmethod
    def _enrollment(row: sqlite3.Row) -> Enrollment:
        return Enrollment(
            _role(row["role"]), row["name"], row["user_id"], row["created_by"], row["expires_at"]
        )

    def peek(self, code: str) -> Enrollment | None:
        """使える値なら中身を返す（使ったことにはしない）。"""
        with self.database.connect() as db:
            row = self._valid(db, code)
        return self._enrollment(row) if row else None

    def consume(self, code: str) -> Enrollment | None:
        """値を使ったことにして中身を返す。同時に2回使われても、1回だけが成功する。"""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._valid(db, code)
            if row is None:
                return None
            db.execute(
                "UPDATE enrollments SET used_at = ? WHERE id = ?", (_stamp(self.clock()), row["id"])
            )
        return self._enrollment(row)


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
