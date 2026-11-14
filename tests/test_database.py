"""スキーマの版管理と移行処理を、古いスキーマのデータベースで検証する。

利用者の手元には版管理を入れる前の events.sqlite3 が残っているので、
その形のデータベースを一時ディレクトリに作り、データを失わずに最新になることを確かめる。
"""

import sqlite3

import numpy as np
import pytest

from poop_detector.records.database import (
    LATEST_VERSION,
    MIGRATIONS,
    TABLES_V1,
    Database,
    migrate,
    schema_version,
)
from poop_detector.records.events import Store
from poop_detector.video.clips import ClipStore

# 版管理を入れる直前（0.1.0 の開発中）のスキーマ。Store と ClipStore の初期化が作っていた形。
PRE_VERSIONING = """
CREATE TABLE events (
    id TEXT PRIMARY KEY, timestamp TEXT, kind TEXT, score REAL,
    detection_started TEXT, notified_at TEXT, processing_ms REAL,
    image TEXT, feedback INTEGER, acknowledged INTEGER DEFAULT 0,
    actual_start TEXT, actual_end TEXT, location TEXT);
CREATE TABLE monitor_state (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT, event_id TEXT);
CREATE TABLE excretion_labels (
    event_id TEXT PRIMARY KEY, excretion_type TEXT NOT NULL, note TEXT NOT NULL);
CREATE TABLE detection_reference (id INTEGER PRIMARY KEY CHECK(id=1), signature TEXT, image BLOB);
CREATE TABLE clips (
    id TEXT PRIMARY KEY, event_id TEXT, source TEXT, requested_at TEXT,
    status TEXT, path TEXT, start_at TEXT, end_at TEXT, duration REAL,
    fps REAL, trigger_offset REAL, pre_available REAL, post_available REAL,
    interrupted INTEGER DEFAULT 0, error TEXT, label TEXT,
    actual_start_offset REAL, actual_end_offset REAL, activity TEXT DEFAULT 'other',
    note TEXT DEFAULT '', scene_signature TEXT, location TEXT);
CREATE INDEX clips_by_status_time ON clips(status, requested_at);
CREATE TABLE capture_status (id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL, healthy INTEGER);
CREATE TABLE codex_assessments (
    clip_id TEXT PRIMARY KEY, label TEXT, activity TEXT, start_seconds REAL,
    end_seconds REAL, reason TEXT, observed_at TEXT, error TEXT,
    assessed_at TEXT NOT NULL, notice_claimed INTEGER DEFAULT 0, notified_at TEXT,
    location TEXT);
CREATE TABLE clip_confirmations (clip_id TEXT PRIMARY KEY, event_id TEXT NOT NULL);
CREATE TABLE clip_protections (clip_id TEXT PRIMARY KEY);
CREATE TABLE circling_reviews (
    clip_id TEXT PRIMARY KEY, candidates TEXT NOT NULL,
    sampled INTEGER NOT NULL, located INTEGER NOT NULL);
"""

# 排泄の場所（location 列）を足す前の、さらに古いスキーマ。動画の表もまだない。
BEFORE_LOCATION = """
CREATE TABLE events (
    id TEXT PRIMARY KEY, timestamp TEXT, kind TEXT, score REAL,
    detection_started TEXT, notified_at TEXT, processing_ms REAL,
    image TEXT, feedback INTEGER, acknowledged INTEGER DEFAULT 0,
    actual_start TEXT, actual_end TEXT);
CREATE TABLE excretion_labels (
    event_id TEXT PRIMARY KEY, excretion_type TEXT NOT NULL, note TEXT NOT NULL);
"""


def create(path, script, rows=()):
    db = sqlite3.connect(path)
    db.executescript(script)
    for sql, values in rows:
        db.execute(sql, values)
    db.commit()
    db.close()


def columns(path, table):
    db = sqlite3.connect(path)
    try:
        return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
    finally:
        db.close()


def tables(path):
    db = sqlite3.connect(path)
    try:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()


def version(path):
    db = sqlite3.connect(path)
    try:
        return schema_version(db)
    finally:
        db.close()


EVENT = (
    "INSERT INTO events (id,timestamp,kind,feedback,actual_start) VALUES (?,?,?,?,?)",
    ("e1", "2026-11-01T00:00:00+00:00", "MANUAL", 1, "2026-11-01T00:00:00+00:00"),
)
LABEL = ("INSERT INTO excretion_labels VALUES (?,?,?)", ("e1", "feces", "朝の記録"))


def test_new_database_is_created_at_the_latest_version(tmp_path):
    Store(tmp_path)
    path = tmp_path / "events.sqlite3"
    assert version(path) == LATEST_VERSION
    renamed = {"codex_assessments": "video_assessments"}
    for table, expected in TABLES_V1.items():
        extra = ["provider", "model"] if table == "codex_assessments" else []
        assert columns(path, renamed.get(table, table)) == [name for name, _ in expected] + extra


def test_pre_versioning_database_keeps_its_data(tmp_path):
    path = tmp_path / "events.sqlite3"
    clip = (
        "INSERT INTO clips (id,source,requested_at,status,label,note,location) "
        "VALUES (?,?,?,?,?,?,?)",
        ("c1", "manual", "2026-11-01T00:00:00+00:00", "ready", "feces", "メモ", "tray"),
    )
    link = ("INSERT INTO clip_confirmations VALUES (?,?)", ("c1", "e1"))
    assessment = (
        "INSERT INTO codex_assessments (clip_id,label,reason,assessed_at) VALUES (?,?,?,?)",
        ("c1", "feces", "新しい便", "2026-11-01T00:00:00+00:00"),
    )
    create(path, PRE_VERSIONING, [EVENT, LABEL, clip, link, assessment])
    assert version(path) == 0

    store, clips = Store(tmp_path), ClipStore(tmp_path)

    assert version(path) == LATEST_VERSION
    (event,) = store.list()
    assert (event["id"], event["excretion_type"], event["note"]) == ("e1", "feces", "朝の記録")
    row = clips.get("c1")
    assert row is not None
    assert (row["label"], row["note"], row["location"]) == ("feces", "メモ", "tray")
    assert clips.labels.confirmation("c1") == "e1"
    # 版2で、判定の表の名前から判定器の製品名を外した。データはそのまま移る。
    saved = clips.assessments.get("c1")
    assert saved is not None
    assert (saved["label"], saved["reason"]) == ("feces", "新しい便")
    assert (saved["provider"], saved["model"]) == ("codex", None)
    assert "codex_assessments" not in tables(path)


def test_database_before_location_column_gains_missing_columns_and_tables(tmp_path):
    path = tmp_path / "events.sqlite3"
    create(path, BEFORE_LOCATION, [EVENT, LABEL])

    store = Store(tmp_path)

    assert "location" in columns(path, "events")
    assert columns(path, "clips")[-1] == "location"
    (event,) = store.list()
    assert event["location"] is None
    assert event["note"] == "朝の記録"
    # 足した列とテーブルを、そのまま使える。
    store.classify("e1", "feces", location="outside")
    assert store.list()[0]["location"] == "outside"
    clip = ClipStore(tmp_path).request("manual")
    assert ClipStore(tmp_path).get(clip) is not None


def test_reopening_does_not_run_migrations_again(tmp_path):
    Store(tmp_path)
    db = sqlite3.connect(tmp_path / "events.sqlite3")
    try:
        assert migrate(db) == 0
        assert schema_version(db) == LATEST_VERSION
    finally:
        db.close()


def test_failed_migration_leaves_the_database_unchanged(tmp_path):
    """移行できない形なら、途中まで変えずに元のまま残す。"""
    path = tmp_path / "events.sqlite3"
    # NOT NULL の列（assessed_at）が欠けた判定の表は、ALTER TABLE で補えない。
    broken = BEFORE_LOCATION + "CREATE TABLE codex_assessments (clip_id TEXT PRIMARY KEY);"
    create(path, broken, [EVENT, LABEL])
    before = columns(path, "events")

    with pytest.raises(ValueError, match="codex_assessments"):
        Database(path)

    assert version(path) == 0
    assert columns(path, "events") == before
    assert "clips" not in tables(path)


def test_database_from_a_newer_version_is_refused(tmp_path):
    path = tmp_path / "events.sqlite3"
    create(path, PRE_VERSIONING + f"PRAGMA user_version = {LATEST_VERSION + 1};")

    with pytest.raises(ValueError, match="新しい poop-detector"):
        Store(tmp_path)

    assert version(path) == LATEST_VERSION + 1


def test_store_and_clip_store_share_one_connection_setup(tmp_path):
    store = Store(tmp_path)
    clips = ClipStore(tmp_path)
    event = store.record(np.zeros((4, 4, 3), np.uint8), 0.1, None, 1)
    with clips.connect() as db:
        assert db.execute("SELECT id FROM events").fetchone()["id"] == event
    assert clips.store.database.path == store.database.path


def test_store_no_longer_exposes_unused_monitor_state(tmp_path):
    """監視の状態は誰も読まないので書かない。古い表は既存データを守るため残す。"""
    store = Store(tmp_path)
    assert not hasattr(store, "monitor_state")
    assert not hasattr(store, "set_monitor_state")
    assert "monitor_state" in tables(tmp_path / "events.sqlite3")


def test_version_3_database_gains_the_gui_session_table(tmp_path):
    """版3（判定器の記録まで）のデータベースに、GUI のセッションの表を足す。データは残る。"""
    path = tmp_path / "events.sqlite3"
    create(path, PRE_VERSIONING, [EVENT, LABEL])
    db = sqlite3.connect(path)
    try:
        for migration in MIGRATIONS[:3]:
            migration(db)
        db.execute("PRAGMA user_version = 3")
        db.commit()
    finally:
        db.close()
    assert "gui_sessions" not in tables(path)

    store = Store(tmp_path)

    assert version(path) == LATEST_VERSION
    assert columns(path, "gui_sessions") == ["id", "created_at", "last_seen", "user_id"]
    assert store.list()[0]["note"] == "朝の記録"


def test_version_4_database_gains_the_account_tables(tmp_path):
    """版4（GUI のセッションまで）のデータベースに、ユーザー・パスキー・登録用 URL の表を足す。"""
    path = tmp_path / "events.sqlite3"
    create(path, PRE_VERSIONING, [EVENT, LABEL])
    db = sqlite3.connect(path)
    try:
        for migration in MIGRATIONS[:4]:
            migration(db)
        db.execute("PRAGMA user_version = 4")
        db.commit()
    finally:
        db.close()

    store = Store(tmp_path)

    assert version(path) == LATEST_VERSION
    assert {"users", "passkeys", "enrollments", "gui_sessions"} <= tables(path)
    assert store.list()[0]["note"] == "朝の記録"


def test_version_10_database_gains_the_benchmark_candidates_table(tmp_path):
    """版10（監視の停止の履歴まで）のデータベースに、評価の候補とタグの表を足す。データは残る。"""
    path = tmp_path / "events.sqlite3"
    create(path, PRE_VERSIONING, [EVENT, LABEL])
    db = sqlite3.connect(path)
    try:
        for migration in MIGRATIONS[:10]:
            migration(db)
        db.execute("PRAGMA user_version = 10")
        db.execute("INSERT INTO clips (id,status,label,duration) VALUES ('c1','ready','feces',30)")
        db.commit()
    finally:
        db.close()
    assert "benchmark_candidates" not in tables(path)

    clips = ClipStore(tmp_path)

    assert version(path) == LATEST_VERSION
    assert columns(path, "benchmark_candidates") == ["clip_id", "state", "tags", "updated_at"]
    assert clips.get("c1")["label"] == "feces"
    assert clips.store.list()[0]["note"] == "朝の記録"
    db = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO benchmark_candidates VALUES ('c1','maybe','[]','2026-11-13')")
    finally:
        db.close()


def test_feeding_details_migration_keeps_old_care_entries(tmp_path):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:13]:
            migration(db)
        db.execute("PRAGMA user_version = 13")
        db.execute(
            "INSERT INTO care_entries VALUES (?, ?, ?, ?, ?)",
            ("old", "2024-03-01T08:00:00+00:00", "feeding", "家族", "旧記録"),
        )
    with Database(path).connect() as db:
        row = db.execute("SELECT note, food, amount_dg FROM care_entries WHERE id='old'").fetchone()
        assert tuple(row) == ("旧記録", "", None)
