"""events.sqlite3 の接続とスキーマの版の管理。

スキーマの版は SQLite の PRAGMA user_version に記録する。開くときに、記録された版より
新しい移行処理を順に適用して最新にする。スキーマを変えるときは、MIGRATIONS の末尾に
移行処理を1つ足す（既存の移行処理は書き換えない）。

版0（user_version が未記録）は、版管理を入れる前のデータベースか、新しいデータベース。
版1への移行は、いまのテーブルをすべて作り、既存のテーブルに足りない列を加える。
"""

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

from ..errors import IncompatibleDataError

# 版1のテーブル（版1の時点の名前）。(列名, 型と既定値) の順で、新しいテーブルはこの順に作る。
# 既存のテーブルに足りない列は、ALTER TABLE で末尾に加える。
TABLES_V1: dict[str, Sequence[tuple[str, str]]] = {
    "events": [
        ("id", "TEXT PRIMARY KEY"),
        ("timestamp", "TEXT"),
        ("kind", "TEXT"),
        ("score", "REAL"),
        ("detection_started", "TEXT"),
        ("notified_at", "TEXT"),
        ("processing_ms", "REAL"),
        ("image", "TEXT"),
        ("feedback", "INTEGER"),
        ("acknowledged", "INTEGER DEFAULT 0"),
        ("actual_start", "TEXT"),
        ("actual_end", "TEXT"),
        ("location", "TEXT"),
    ],
    # 版1で作った表。いまは読み書きしないが、既存のデータベースを変えないよう残す。
    "monitor_state": [
        ("id", "INTEGER PRIMARY KEY CHECK(id=1)"),
        ("state", "TEXT"),
        ("event_id", "TEXT"),
    ],
    "excretion_labels": [
        ("event_id", "TEXT PRIMARY KEY"),
        ("excretion_type", "TEXT NOT NULL"),
        ("note", "TEXT NOT NULL"),
    ],
    "detection_reference": [
        ("id", "INTEGER PRIMARY KEY CHECK(id=1)"),
        ("signature", "TEXT"),
        ("image", "BLOB"),
    ],
    "clips": [
        ("id", "TEXT PRIMARY KEY"),
        ("event_id", "TEXT"),
        ("source", "TEXT"),
        ("requested_at", "TEXT"),
        ("status", "TEXT"),
        ("path", "TEXT"),
        ("start_at", "TEXT"),
        ("end_at", "TEXT"),
        ("duration", "REAL"),
        ("fps", "REAL"),
        ("trigger_offset", "REAL"),
        ("pre_available", "REAL"),
        ("post_available", "REAL"),
        ("interrupted", "INTEGER DEFAULT 0"),
        ("error", "TEXT"),
        ("label", "TEXT"),
        ("actual_start_offset", "REAL"),
        ("actual_end_offset", "REAL"),
        ("activity", "TEXT DEFAULT 'other'"),
        ("note", "TEXT DEFAULT ''"),
        ("scene_signature", "TEXT"),
        ("location", "TEXT"),
    ],
    "capture_status": [
        ("id", "INTEGER PRIMARY KEY CHECK(id=1)"),
        ("heartbeat", "REAL"),
        ("healthy", "INTEGER"),
    ],
    "codex_assessments": [
        ("clip_id", "TEXT PRIMARY KEY"),
        ("label", "TEXT"),
        ("activity", "TEXT"),
        ("start_seconds", "REAL"),
        ("end_seconds", "REAL"),
        ("reason", "TEXT"),
        ("observed_at", "TEXT"),
        ("error", "TEXT"),
        ("assessed_at", "TEXT NOT NULL"),
        ("notice_claimed", "INTEGER DEFAULT 0"),
        ("notified_at", "TEXT"),
        ("location", "TEXT"),
    ],
    "clip_confirmations": [
        ("clip_id", "TEXT PRIMARY KEY"),
        ("event_id", "TEXT NOT NULL"),
    ],
    "clip_protections": [("clip_id", "TEXT PRIMARY KEY")],
    "circling_reviews": [
        ("clip_id", "TEXT PRIMARY KEY"),
        ("candidates", "TEXT NOT NULL"),
        ("sampled", "INTEGER NOT NULL"),
        ("located", "INTEGER NOT NULL"),
    ],
}


def _create_or_complete(
    db: sqlite3.Connection, table: str, columns: Sequence[tuple[str, str]]
) -> None:
    """テーブルがなければ作り、あれば足りない列を加える。

    名前は TABLES_V1 の固定の文字列だけで、利用者の入力は入らない。
    """
    definition = ", ".join(f"{name} {kind}" for name, kind in columns)
    db.execute(f"CREATE TABLE IF NOT EXISTS {table} ({definition})")
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, kind in columns:
        if name not in existing:
            if "NOT NULL" in kind or "PRIMARY KEY" in kind:
                raise IncompatibleDataError(f"{table} テーブルを最新の形に移行できません")
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def migrate_to_v1(db: sqlite3.Connection) -> None:
    """版管理を入れる前のスキーマを、版1としてそろえる。"""
    for table, columns in TABLES_V1.items():
        _create_or_complete(db, table, columns)
    db.execute("CREATE INDEX IF NOT EXISTS clips_by_status_time ON clips(status, requested_at)")


def migrate_to_v2(db: sqlite3.Connection) -> None:
    """動画の自動判定の表から、判定器の製品名を外す（codex_assessments → video_assessments）。"""
    db.execute("ALTER TABLE codex_assessments RENAME TO video_assessments")


def migrate_to_v3(db: sqlite3.Connection) -> None:
    """自動判定に判定器とモデルを記録する。既存の判定器は Codex CLI。"""
    db.execute("ALTER TABLE video_assessments ADD COLUMN provider TEXT NOT NULL DEFAULT 'codex'")
    db.execute("ALTER TABLE video_assessments ADD COLUMN model TEXT")


def migrate_to_v4(db: sqlite3.Connection) -> None:
    """GUI のセッション。id は Cookie の値の SHA-256 で、値そのものは保存しない。

    時刻は UTC の ISO 8601。user_id はユーザー認証を入れるまで空。
    """
    db.execute(
        """CREATE TABLE gui_sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            user_id TEXT
        )"""
    )


def migrate_to_v5(db: sqlite3.Connection) -> None:
    """GUI のユーザー、パスキー、登録用 URL。

    enrollments.id は登録用 URL の値の SHA-256 で、値そのものは保存しない。
    時刻は UTC の ISO 8601。
    """
    db.execute(
        """CREATE TABLE users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
            created_at TEXT NOT NULL,
            last_login TEXT,
            disabled_at TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE passkeys (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            rp_id TEXT NOT NULL,
            public_key BLOB NOT NULL,
            sign_count INTEGER NOT NULL,
            transports TEXT NOT NULL,
            label TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used TEXT
        )"""
    )
    db.execute("CREATE INDEX passkeys_by_user ON passkeys(user_id)")
    db.execute(
        """CREATE TABLE enrollments (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
            name TEXT,
            user_id TEXT REFERENCES users(id),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        )"""
    )


def migrate_to_v6(db: sqlite3.Connection) -> None:
    """ユーザーと招待にメールアドレスを追加し、既存の ID を維持する。"""
    db.execute("ALTER TABLE users ADD COLUMN email TEXT")
    db.execute("CREATE UNIQUE INDEX users_by_email ON users(email)")
    db.execute("ALTER TABLE enrollments ADD COLUMN email TEXT")
    db.execute("ALTER TABLE enrollments ADD COLUMN revoked_at TEXT")
    db.execute("CREATE INDEX enrollments_by_email ON enrollments(email)")


def migrate_to_v7(db: sqlite3.Connection) -> None:
    """管理者の操作の監査ログ。

    actor はユーザーの ID か、監視するマシンでのコマンドの操作を表す "cli"。
    detail は利用者に見せる説明で、登録用 URL や認証情報は入れない。
    """
    db.execute(
        """CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL
        )"""
    )


def migrate_to_v8(db: sqlite3.Connection) -> None:
    """GUI の表示の選択。theme が NULL の人は、管理者が決めた既定のテーマを使う。"""
    db.execute("ALTER TABLE users ADD COLUMN theme TEXT")
    db.execute("ALTER TABLE users ADD COLUMN appearance TEXT NOT NULL DEFAULT 'auto'")


def migrate_to_v9(db: sqlite3.Connection) -> None:
    """Web Push の購読と、人ごとの受け取りの選択。

    endpoint はブラウザーの購読の宛先（推測できない URL）で、p256dh と auth は通知の暗号化に使う
    ブラウザーの公開鍵と秘密の値。ログや画面には出さない。
    """
    db.execute(
        """CREATE TABLE push_subscriptions (
            endpoint TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    db.execute("ALTER TABLE users ADD COLUMN push_alerts INTEGER NOT NULL DEFAULT 1")
    db.execute("ALTER TABLE users ADD COLUMN push_early INTEGER NOT NULL DEFAULT 0")


def migrate_to_v10(db: sqlite3.Connection) -> None:
    """監視の停止とカメラの不調の履歴（outages）と、管理者の見張りの通知の受け取りの選択。

    kind は monitor（監視の停止）か camera（カメラの不調）。ended_at が NULL の行は続いている停止で、
    種類ごとに1行までにする。時刻は UTC の ISO 8601。
    """
    db.execute(
        """CREATE TABLE outages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('monitor', 'camera')),
            started_at TEXT NOT NULL,
            notified_at TEXT,
            ended_at TEXT
        )"""
    )
    db.execute("CREATE INDEX outages_by_start ON outages(started_at)")
    db.execute("CREATE UNIQUE INDEX outages_open ON outages(kind) WHERE ended_at IS NULL")
    db.execute("ALTER TABLE users ADD COLUMN push_watchdog INTEGER NOT NULL DEFAULT 1")


# MIGRATIONS[i] は版 i から版 i+1 への移行。末尾に足していく。
MIGRATIONS: Sequence[Callable[[sqlite3.Connection], None]] = (
    migrate_to_v1,
    migrate_to_v2,
    migrate_to_v3,
    migrate_to_v4,
    migrate_to_v5,
    migrate_to_v6,
    migrate_to_v7,
    migrate_to_v8,
    migrate_to_v9,
    migrate_to_v10,
)
LATEST_VERSION = len(MIGRATIONS)


def schema_version(db: sqlite3.Connection) -> int:
    version: int = db.execute("PRAGMA user_version").fetchone()[0]
    return version


def migrate(db: sqlite3.Connection) -> int:
    """データベースを最新の版にし、適用した移行の数を返す。

    監視と GUI が同時に開いても二重に適用しないよう、書き込みの排他を取ってから版を読む。
    途中で失敗した場合は、その接続の変更をすべて取り消す。
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        version = schema_version(db)
        if version > LATEST_VERSION:
            raise IncompatibleDataError(
                "データベースがこの版より新しい poop-detector で作られています。"
                "poop-detector を更新してください"
            )
        for migration in MIGRATIONS[version:]:
            migration(db)
        # PRAGMA には ? のプレースホルダーを使えない。値は整数の定数だけ。
        db.execute(f"PRAGMA user_version = {LATEST_VERSION}")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return LATEST_VERSION - version


class Database:
    """events.sqlite3 への接続。初めて開くときにスキーマを最新にする。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        db = self.connect()
        try:
            if schema_version(db) != LATEST_VERSION:
                migrate(db)
        finally:
            db.close()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db


def events_database(root: str | Path) -> Database:
    """データの保存先の events.sqlite3。保存先のディレクトリがなければ作る。"""
    Path(root).mkdir(parents=True, exist_ok=True)
    return Database(Path(root) / "events.sqlite3")
