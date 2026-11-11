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


# MIGRATIONS[i] は版 i から版 i+1 への移行。末尾に足していく。
MIGRATIONS: Sequence[Callable[[sqlite3.Connection], None]] = (
    migrate_to_v1,
    migrate_to_v2,
    migrate_to_v3,
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
    """データの保存先の events.sqlite3。"""
    return Database(Path(root) / "events.sqlite3")
