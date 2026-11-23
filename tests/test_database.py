"""スキーマの版管理と移行処理を、古いスキーマのデータベースで検証する。

利用者の手元には版管理を入れる前の events.sqlite3 が残っているので、
その形のデータベースを一時ディレクトリに作り、データを失わずに最新になることを確かめる。
"""

import sqlite3
from datetime import date

import numpy as np
import pytest

from poop_detector.features.outages import OutageLog
from poop_detector.records.database import (
    LATEST_VERSION,
    MIGRATIONS,
    TABLES_V1,
    Database,
    migrate,
    schema_version,
)
from poop_detector.records.events import Store
from poop_detector.records.weights import birth_date, save_birth_date
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
    assessment_columns = [
        "provider",
        "model",
        "endpoint_change",
        "stool_seen_at",
        "stool_clear_at",
        "stool_notified_at",
        "notice_label",
        "stool_notice_claimed",
        "stool_condition",
    ]
    added_columns = {
        "codex_assessments": assessment_columns,
        "clips": ["tray_stay_seconds", "stool_condition"],
    }
    for table, expected in TABLES_V1.items():
        extra = added_columns.get(table, [])
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
    assert columns(path, "clips")[-3:] == ["location", "tray_stay_seconds", "stool_condition"]
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


def test_judge_runs_migration_counts_old_assessments_once(tmp_path):
    """版14のデータベースに判定器の呼び出しの記録を足し、これまでの判定を1回ずつ数える。"""
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:14]:
            migration(db)
        db.execute("PRAGMA user_version = 14")
        db.execute("INSERT INTO clips (id,status,duration) VALUES ('c1','ready',30)")
        db.execute("INSERT INTO clips (id,status,duration) VALUES ('c2','ready',30)")
        db.execute(
            "INSERT INTO video_assessments (clip_id,label,assessed_at,provider,model)"
            " VALUES ('c1','none','2026-11-01T00:00:00+00:00','claude','model-b')"
        )
        db.execute(
            "INSERT INTO video_assessments (clip_id,error,assessed_at)"
            " VALUES ('c2','失敗','2026-11-02T00:00:00+00:00')"
        )
    clips = ClipStore(tmp_path)

    assert version(path) == LATEST_VERSION
    assert clips.assessments.get("c1")["label"] == "none"
    usage = {(row.provider, row.model): row for row in clips.judge_runs.usage()}
    assert (usage["claude", "model-b"].total, usage["claude", "model-b"].failures) == (1, 0)
    assert usage["codex", None].purpose == "review"
    assert (usage["codex", None].total, usage["codex", None].failures) == (1, 1)
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO judge_runs (provider,purpose,ran_at,failed) VALUES ('codex','x','t',0)"
        )


def test_version_15_database_accepts_judge_outages_and_keeps_old_ones(tmp_path):
    """版15の停止の履歴を残したまま、停止の種類に判定器（judge）を加える。"""
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:15]:
            migration(db)
        db.execute("PRAGMA user_version = 15")
        db.execute(
            "INSERT INTO outages (kind, started_at, notified_at, ended_at) VALUES"
            " ('camera', '2026-11-01T00:00:00+00:00', '2026-11-01T00:05:00+00:00',"
            " '2026-11-01T01:00:00+00:00'), ('monitor', '2026-11-02T00:00:00+00:00', NULL, NULL)"
        )
    log = OutageLog(Database(path))

    assert version(path) == LATEST_VERSION
    assert [(o.id, o.kind, o.notified_at, o.ended_at) for o in log.recent()] == [
        (2, "monitor", None, None),
        (1, "camera", "2026-11-01T00:05:00+00:00", "2026-11-01T01:00:00+00:00"),
    ]
    judge, created = log.begin("judge", "2026-11-03T00:00:00+00:00")
    assert (judge.id, created) == (3, True)
    # 続いている停止は、種類ごとに1行までのまま。
    assert log.begin("monitor", "2026-11-03T00:00:00+00:00")[1] is False
    with sqlite3.connect(path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO outages (kind, started_at) VALUES ('other', '2026-11-03')")


def test_reentry_migration_preserves_positions_visits_and_old_notice_reservations(tmp_path):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:18]:
            migration(db)
        db.execute("PRAGMA user_version=18")
        db.execute("INSERT INTO events (id,timestamp) VALUES (?,?)", EVENT[1][:2])
        db.execute(
            """INSERT INTO reentry_waste (event_id,configuration_id,status,position_x,position_y)
               VALUES ('e1','layout','uncollected',0.3,0.4)"""
        )
        db.execute(
            "INSERT INTO reentry_visits (event_id,started_at,risk) VALUES ('e1',?,'HIGH')",
            (EVENT[1][1],),
        )
        db.execute("INSERT INTO reentry_notices VALUES ('e1','high')")

    with Database(path).connect() as db:
        assert tuple(
            db.execute(
                "SELECT status,position_x,position_y,observed_until FROM reentry_waste"
            ).fetchone()
        ) == ("uncollected", 0.3, 0.4, None)
        assert db.execute("SELECT risk FROM reentry_visits").fetchone()["risk"] == "HIGH"
        assert tuple(
            db.execute("SELECT status,attempts,next_attempt_at FROM reentry_notices").fetchone()
        ) == ("unknown", 1, None)


def test_version_20_visits_gain_foot_quality_counts_without_losing_risk(tmp_path):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:20]:
            migration(db)
        db.execute("PRAGMA user_version=20")
        db.execute("INSERT INTO events (id,timestamp) VALUES (?,?)", EVENT[1][:2])
        db.execute(
            """INSERT INTO reentry_waste (event_id,configuration_id,status)
               VALUES ('e1','layout','uncollected')"""
        )
        db.execute(
            "INSERT INTO reentry_visits (event_id,started_at,risk,distance) VALUES ('e1',?,'HIGH',0.01)",
            (EVENT[1][1],),
        )

    with Database(path).connect() as db:
        row = db.execute(
            "SELECT risk,distance,foot_samples,low_foot_samples,high_foot_quality FROM reentry_visits"
        ).fetchone()
    # 品質を記録する前の訪問は、標本数 0（不明）として残す。
    assert tuple(row) == ("HIGH", 0.01, 0, 0, None)


def test_version_21_assessments_gain_the_endpoint_change_without_losing_labels(tmp_path):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:21]:
            migration(db)
        db.execute("PRAGMA user_version=21")
        db.execute(
            "INSERT INTO clips (id,status,requested_at,start_at,duration) VALUES ('c1','ready',?,?,40)",
            ("2026-11-15T00:00:00+00:00", "2026-11-15T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO video_assessments (clip_id,label,assessed_at) VALUES ('c1','urine',?)",
            ("2026-11-15T00:01:00+00:00",),
        )

    with Database(path).connect() as db:
        row = db.execute("SELECT label,endpoint_change FROM video_assessments").fetchone()
    # 指標を記録する前の判定は、値なし（未計測）として残す。
    assert tuple(row) == ("urine", None)


def test_version_22_gains_review_dismissals_without_losing_clips(tmp_path):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:22]:
            migration(db)
        db.execute("PRAGMA user_version=22")
        db.execute(
            "INSERT INTO clips (id,status,requested_at,duration) VALUES ('c1','ready',?,30)",
            ("2026-11-15T00:00:00+00:00",),
        )

    clips = ClipStore(tmp_path)
    assert version(path) == LATEST_VERSION
    assert columns(path, "review_dismissals") == ["clip_id", "dismissed_at"]
    # 外した記録のない動画は、確認待ちから外していないものとして読む。
    (row,) = clips.list(include_assessments=True)
    assert (row["id"], row["review_dismissed"]) == ("c1", 0)


@pytest.mark.parametrize("fail", [False, True])
def test_connection_closes_after_commit_or_rollback(tmp_path, fail):
    database = Database(tmp_path / "test.sqlite3")
    with database.connect() as db:
        db.execute("CREATE TABLE probe(value INTEGER)")
    try:
        with database.connect() as connection:
            connection.execute("INSERT INTO probe VALUES (1)")
            if fail:
                raise ValueError("書き込み中断")
    except ValueError:
        assert fail

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == int(not fail)


def test_feeding_plan_migration_preserves_care_and_disables_reminders_by_default(tmp_path):
    path = tmp_path / "feeding-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:23]:
            migration(db)
        db.execute("PRAGMA user_version = 23")
        db.execute(
            "INSERT INTO care_entries (id, at, kind, actor, note, amount_dg) VALUES "
            "('meal', '2025-02-01T00:00:00+00:00', 'feeding', '家族', '', 250)"
        )
        db.execute(
            "INSERT INTO users (id, name, role, created_at) VALUES ('owner', '家族', 'admin', '')"
        )
    with Database(path).connect() as db:
        assert tuple(db.execute("SELECT id, amount_dg, plan_id FROM care_entries").fetchone()) == (
            "meal",
            250,
            None,
        )
        assert db.execute("SELECT push_feeding FROM users").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM feeding_plans").fetchone()[0] == 0


def test_stool_observation_migration_preserves_existing_notice(tmp_path):
    path = tmp_path / "stool-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:24]:
            migration(db)
        db.execute("PRAGMA user_version = 24")
        db.execute("INSERT INTO clips (id, requested_at, status) VALUES ('clip', '', 'ready')")
        db.execute(
            """INSERT INTO video_assessments (clip_id, label, assessed_at, notified_at, notice_claimed)
            VALUES ('clip', 'feces', '2026-11-19', '2026-11-19', 1)"""
        )
    with Database(path).connect() as db:
        assert tuple(
            db.execute(
                """SELECT label, notified_at, notice_claimed, stool_seen_at, stool_clear_at,
                stool_notice_claimed FROM video_assessments WHERE clip_id='clip'"""
            ).fetchone()
        ) == ("feces", "2026-11-19", 1, None, None, 0)


def test_care_deletion_migration_preserves_existing_plan_record(tmp_path):
    path = tmp_path / "care-deletion-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:25]:
            migration(db)
        db.execute("PRAGMA user_version = 25")
        db.execute("INSERT INTO feeding_plans (id, clock, changed_at) VALUES ('plan', '08:00', '')")
        db.execute(
            """INSERT INTO care_entries (id, at, kind, actor, note, plan_id, plan_day)
            VALUES ('meal', '2026-11-19T00:00:00+00:00', 'feeding', '家族', '朝', 'plan', '2026-11-19')"""
        )
    with Database(path).connect() as db:
        assert tuple(
            db.execute(
                "SELECT note, plan_id, plan_day, deleted_at FROM care_entries WHERE id='meal'"
            ).fetchone()
        ) == ("朝", "plan", "2026-11-19", None)


def test_tray_stay_migration_preserves_an_existing_video_and_assessment(tmp_path):
    path = tmp_path / "tray-stay-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:26]:
            migration(db)
        db.execute("PRAGMA user_version = 26")
        db.execute(
            """INSERT INTO clips (id, source, requested_at, status)
            VALUES ('clip', 'dog_exit', '2026-11-19', 'ready')"""
        )
        db.execute(
            """INSERT INTO video_assessments (clip_id, label, assessed_at)
            VALUES ('clip', 'urine', '2026-11-19')"""
        )
    with Database(path).connect() as db:
        assert tuple(
            db.execute(
                """SELECT c.status, c.tray_stay_seconds, a.label
                FROM clips c JOIN video_assessments a ON a.clip_id=c.id"""
            ).fetchone()
        ) == ("ready", None, "urine")


def test_sleep_migration_preserves_existing_clip(tmp_path):
    path = tmp_path / "sleep-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:27]:
            migration(db)
        db.execute("PRAGMA user_version = 27")
        db.execute(
            """INSERT INTO clips (id, source, requested_at, status)
            VALUES ('clip', 'dog_exit', '2026-11-19', 'ready')"""
        )
    with Database(path).connect() as db:
        assert db.execute("SELECT status FROM clips WHERE id='clip'").fetchone()[0] == "ready"
        tables = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'sleep_%'")
        }
    assert tables == {"sleep_minutes", "sleep_detection_hours"}


def test_failure_kind_migration_keeps_old_judge_runs(tmp_path):
    path = tmp_path / "failure-kind-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:28]:
            migration(db)
        db.execute("PRAGMA user_version = 28")
        db.execute(
            """INSERT INTO judge_runs (provider, purpose, ran_at, failed, input_tokens)
            VALUES ('gemini', 'review', '2026-11-20T00:00:00+00:00', 1, 10)"""
        )
    with Database(path).connect() as db:
        row = db.execute(
            "SELECT provider, failed, input_tokens, failure, failure_detail FROM judge_runs"
        ).fetchone()
    assert tuple(row) == ("gemini", 1, 10, None, None)


def test_purpose_migration_keeps_judge_runs_and_accepts_new_purposes(tmp_path):
    path = tmp_path / "purpose-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:30]:
            migration(db)
        db.execute("PRAGMA user_version = 30")
        db.execute(
            """INSERT INTO judge_runs (provider, purpose, ran_at, failed, input_tokens, failure)
            VALUES ('codex', 'review', '2026-11-20T00:00:00+00:00', 1, 10, 'timeout')"""
        )
    with Database(path).connect() as db:
        db.execute(
            "INSERT INTO judge_runs (provider, purpose, ran_at, failed)"
            " VALUES ('codex', 'advice', '2026-11-21T00:00:00+00:00', 0),"
            " ('codex', 'food_lookup', '2026-11-21T00:00:01+00:00', 0)"
        )
        indexes = {r[1] for r in db.execute("PRAGMA index_list(judge_runs)")}
        assert "judge_runs_by_time" in indexes
        rows = db.execute(
            "SELECT purpose, failed, input_tokens, failure FROM judge_runs ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("review", 1, 10, "timeout"),
        ("advice", 0, None, None),
        ("food_lookup", 0, None, None),
    ]


def test_screening_migration_keeps_clips_and_adds_the_table(tmp_path):
    path = tmp_path / "screening-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:29]:
            migration(db)
        db.execute("PRAGMA user_version = 29")
        db.execute(
            """INSERT INTO clips (id, source, requested_at, status, tray_stay_seconds)
            VALUES ('clip', 'dog_visit', '2026-11-21', 'ready', 1.0)"""
        )
    with Database(path).connect() as db:
        row = db.execute("SELECT status, tray_stay_seconds FROM clips").fetchone()
        assert tuple(row) == ("ready", 1.0)
        assert db.execute("SELECT COUNT(*) FROM clip_screenings").fetchone()[0] == 0


def test_v30_database_keeps_profile_and_weights_after_v31(tmp_path):
    path = tmp_path / "feeding-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:30]:
            migration(db)
        db.execute("PRAGMA user_version = 30")
        db.execute("INSERT INTO pet_profile VALUES (1, '2026-06-15')")
        db.execute("INSERT INTO weights VALUES ('w1', '2026-11-01', 3200, 'メモ')")
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        row = db.execute("SELECT birth, breed, size_class, food_id FROM pet_profile").fetchone()
        assert tuple(row) == ("2026-06-15", None, None, None)
        assert db.execute("SELECT grams FROM weights").fetchone()[0] == 3200
        assert db.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM food_lookups").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM advice_threads").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM advice_messages").fetchone()[0] == 0
        indexes = {r[1] for r in db.execute("PRAGMA index_list(advice_messages)")}
        assert "advice_messages_status" in indexes
        indexes = {r[1] for r in db.execute("PRAGMA index_list(food_lookups)")}
        assert "food_lookups_status" in indexes


def test_save_birth_date_after_profile_columns_are_added(tmp_path):
    database = Database(tmp_path / "birth.sqlite3")
    save_birth_date(database, "2026-06-15")
    assert birth_date(database) == date(2026, 6, 15)


def test_v31_database_gains_the_records_index_and_keeps_events(tmp_path):
    """版31のデータベースに、確定記録を開始の時刻で探す索引を足す。記録は残る。"""
    path = tmp_path / "records-index.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:31]:
            migration(db)
        db.execute("PRAGMA user_version = 31")
        db.execute(
            "INSERT INTO events (id,timestamp,kind,feedback,actual_start) VALUES (?,?,?,?,?)",
            ("e1", "2026-11-01T01:00:00+00:00", "MANUAL", 1, "2026-11-01T01:00:00+00:00"),
        )
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        assert db.execute("SELECT id FROM events").fetchone()[0] == "e1"
        indexes = {r[1] for r in db.execute("PRAGMA index_list(events)")}
        assert "events_records_by_start" in indexes


def test_v32_database_keeps_care_entries_and_adds_sheet_wear(tmp_path):
    path = tmp_path / "sheet-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:32]:
            migration(db)
        db.execute("PRAGMA user_version = 32")
        db.execute(
            "INSERT INTO care_entries (id, at, kind, actor, note) "
            "VALUES ('c1', '2026-11-01T00:00:00+00:00', 'sheet', '家族', '')"
        )
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        assert db.execute("SELECT id FROM care_entries").fetchone()[0] == "c1"
        assert db.execute("SELECT COUNT(*) FROM sheet_wear").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO sheet_wear (at, kind, ratio) VALUES ('t', 'event', 2)")


def test_v33_database_keeps_sheet_wear_and_adds_floor_objects(tmp_path):
    path = tmp_path / "floor-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:33]:
            migration(db)
        db.execute("PRAGMA user_version = 33")
        db.execute("INSERT INTO sheet_wear (at, kind, ratio) VALUES ('t', 'event', 0.1)")
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        assert db.execute("SELECT ratio FROM sheet_wear").fetchone()[0] == 0.1
        assert db.execute("SELECT COUNT(*) FROM floor_objects").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO floor_objects (id, detected_at, x1, y1, x2, y2, area_ratio, label) "
                "VALUES ('a', 't', 0, 0, 1, 1, 0.1, 'bad')"
            )


def test_v34_database_keeps_floor_objects_and_adds_trend_notices(tmp_path):
    path = tmp_path / "trend-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:34]:
            migration(db)
        db.execute("PRAGMA user_version = 34")
        db.execute(
            "INSERT INTO floor_objects (id, detected_at, x1, y1, x2, y2, area_ratio) "
            "VALUES ('a', 't', 0, 0, 1, 1, 0.1)"
        )
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        assert db.execute("SELECT id FROM floor_objects").fetchone()[0] == "a"
        db.execute("INSERT INTO trend_notices VALUES ('2026-11-20', 'sleep', 'low', 't')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO trend_notices VALUES ('2026-11-20', 'sleep', 'low', 't')")


def test_v35_database_keeps_trend_notices_and_adds_absences(tmp_path):
    path = tmp_path / "absence-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:35]:
            migration(db)
        db.execute("PRAGMA user_version = 35")
        db.execute("INSERT INTO trend_notices VALUES ('2026-11-20', 'sleep', 'low', 't')")
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        assert db.execute("SELECT metric FROM trend_notices").fetchone()[0] == "sleep"
        assert db.execute("SELECT COUNT(*) FROM absences").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO away_mode (id) VALUES (2)")


def test_v36_database_keeps_away_mode_and_opens_the_current_period(tmp_path):
    path = tmp_path / "away-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:36]:
            migration(db)
        db.execute("PRAGMA user_version = 36")
        db.execute(
            "INSERT INTO away_mode (id, away_since, changed_by) "
            "VALUES (1, '2026-11-22T00:00:00+00:00', '家族')"
        )
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        row = db.execute("SELECT started_at, ended_at, started_by FROM away_periods").fetchone()
        assert tuple(row) == ("2026-11-22T00:00:00+00:00", None, "家族")


def test_v37_database_keeps_judge_runs_and_accepts_floor_purpose(tmp_path):
    path = tmp_path / "floor-judge-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:37]:
            migration(db)
        db.execute("PRAGMA user_version = 37")
        db.execute(
            "INSERT INTO judge_runs (provider, purpose, ran_at, failed) "
            "VALUES ('codex', 'advice', '2026-11-22T00:00:00+00:00', 0)"
        )
        db.execute(
            "INSERT INTO floor_objects (id, detected_at, x1, y1, x2, y2, area_ratio) "
            "VALUES ('a', 't', 0, 0, 1, 1, 0.1)"
        )
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        assert db.execute("SELECT purpose FROM judge_runs").fetchone()[0] == "advice"
        db.execute(
            "INSERT INTO judge_runs (provider, purpose, ran_at, failed) VALUES ('codex', 'floor', 't', 0)"
        )
        assert db.execute("SELECT judge_status FROM floor_objects").fetchone()[0] is None
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE floor_objects SET judge_label='cake'")
        indexes = {r[1] for r in db.execute("PRAGMA index_list(judge_runs)")}
        assert "judge_runs_by_time" in indexes


def test_v38_database_adds_vocal_episodes(tmp_path):
    path = tmp_path / "vocal-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:38]:
            migration(db)
        db.execute("PRAGMA user_version = 38")
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        assert db.execute("SELECT COUNT(*) FROM vocal_episodes").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO vocal_episodes (started_at, ended_at, loud_seconds, peak_db) "
                "VALUES ('t', 't', -1, 0)"
            )


def test_v39_database_keeps_judge_runs_and_accepts_digest_purpose(tmp_path):
    path = tmp_path / "digest-summary-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:39]:
            migration(db)
        db.execute("PRAGMA user_version = 39")
        db.execute(
            "INSERT INTO judge_runs (provider, purpose, ran_at, failed) "
            "VALUES ('codex', 'floor', '2026-11-23T00:00:00+00:00', 0)"
        )
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        assert db.execute("SELECT purpose FROM judge_runs").fetchone()[0] == "floor"
        db.execute(
            "INSERT INTO judge_runs (provider, purpose, ran_at, failed) VALUES ('codex', 'digest', 't', 0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO judge_runs (provider, purpose, ran_at, failed) VALUES ('c', 'x', 't', 0)"
            )
        db.execute(
            "INSERT INTO digest_summaries (period_id, status, requested_at) VALUES ('p', 'queued', 't')"
        )
        indexes = {r[1] for r in db.execute("PRAGMA index_list(judge_runs)")}
        assert "judge_runs_by_time" in indexes


def test_v40_database_keeps_users_and_adds_household_and_reminder(tmp_path):
    path = tmp_path / "household-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:40]:
            migration(db)
        db.execute("PRAGMA user_version = 40")
        db.execute(
            "INSERT INTO away_periods (id, started_at) VALUES ('p', '2026-11-23T00:00:00+00:00')"
        )
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        assert db.execute("SELECT reminded_at FROM away_periods").fetchone()[0] is None
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        assert "push_household" in columns


def test_v41_database_keeps_assessments_and_adds_stool_condition(tmp_path):
    path = tmp_path / "stool-condition-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:41]:
            migration(db)
        db.execute("PRAGMA user_version = 41")
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        columns = {row[1] for row in db.execute("PRAGMA table_info(video_assessments)")}
        assert "stool_condition" in columns


def test_v42_database_adds_night_notices(tmp_path):
    path = tmp_path / "night-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:42]:
            migration(db)
        db.execute("PRAGMA user_version = 42")
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        db.execute("INSERT INTO night_notices VALUES ('2026-11-22', 't')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO night_notices VALUES ('2026-11-22', 't')")


def test_v43_database_keeps_clips_and_adds_the_human_stool_condition(tmp_path):
    path = tmp_path / "clip-stool-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:43]:
            migration(db)
        db.execute("PRAGMA user_version = 43")
        db.execute(
            "INSERT INTO clips (id, source, requested_at, status) "
            "VALUES ('c1', 'manual', '2026-11-23T00:00:00+00:00', 'ready')"
        )
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        assert db.execute("SELECT stool_condition FROM clips").fetchone()[0] is None
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE clips SET stool_condition='liquid'")


def test_v44_database_keeps_vocal_episodes_and_adds_model_columns(tmp_path):
    path = tmp_path / "vocal-model-migration.sqlite3"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:44]:
            migration(db)
        db.execute("PRAGMA user_version = 44")
        db.execute(
            "INSERT INTO vocal_episodes (started_at, ended_at, loud_seconds, peak_db) "
            "VALUES ('a', 'b', 2.0, -10.0)"
        )
    database = Database(path)
    with database.connect() as db:
        assert schema_version(db) == LATEST_VERSION
        row = db.execute("SELECT loud_seconds, dog_score, label FROM vocal_episodes").fetchone()
        assert tuple(row) == (2.0, None, None)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE vocal_episodes SET label='cat'")
