"""events.sqlite3 の接続とスキーマの版の管理。

スキーマの版は SQLite の PRAGMA user_version に記録する。開くときに、記録された版より
新しい移行処理を順に適用して最新にする。スキーマを変えるときは、MIGRATIONS の末尾に
移行処理を1つ足す（既存の移行処理は書き換えない）。

版0（user_version が未記録）は、版管理を入れる前のデータベースか、新しいデータベース。
版1への移行は、いまのテーブルをすべて作り、既存のテーブルに足りない列を加える。
"""

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
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


def migrate_to_v11(db: sqlite3.Connection) -> None:
    """判定器の比較に使う評価の候補と条件のタグ（動画の画面で人が選ぶ）。

    state は include（評価に入れる）か exclude（評価から除く）で、NULL は選んでいない録画。
    tags は条件のタグの JSON の配列。時刻は UTC の ISO 8601。
    """
    db.execute(
        """CREATE TABLE benchmark_candidates (
            clip_id TEXT PRIMARY KEY,
            state TEXT CHECK (state IN ('include', 'exclude')),
            tags TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        )"""
    )


def migrate_to_v12(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE pet_profile (id INTEGER PRIMARY KEY CHECK(id=1), birth TEXT)")
    db.execute(
        """CREATE TABLE weights (
            id TEXT PRIMARY KEY, day TEXT NOT NULL,
            grams INTEGER NOT NULL CHECK(grams > 0), note TEXT NOT NULL
        )"""
    )


def migrate_to_v13(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE care_entries (
            id TEXT PRIMARY KEY, at TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('feeding', 'sheet')),
            actor TEXT NOT NULL, note TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX care_entries_at ON care_entries(at)")


def migrate_to_v14(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE care_entries ADD COLUMN food TEXT NOT NULL DEFAULT ''")
    db.execute("ALTER TABLE care_entries ADD COLUMN amount_dg INTEGER CHECK(amount_dg > 0)")


def migrate_to_v15(db: sqlite3.Connection) -> None:
    """判定器を呼んだ記録（使用量の集計用）。これまでの判定は、台帳の1行を1回として数える。"""
    db.execute(
        """CREATE TABLE judge_runs (
        id INTEGER PRIMARY KEY,
        provider TEXT NOT NULL,
        model TEXT,
        purpose TEXT NOT NULL CHECK(purpose IN ('review', 'benchmark')),
        ran_at TEXT NOT NULL,
        failed INTEGER NOT NULL CHECK(failed IN (0, 1)))"""
    )
    db.execute("CREATE INDEX judge_runs_by_time ON judge_runs(ran_at)")
    db.execute(
        """INSERT INTO judge_runs (provider, model, purpose, ran_at, failed)
        SELECT provider, model, 'review', assessed_at, error IS NOT NULL
        FROM video_assessments WHERE assessed_at IS NOT NULL ORDER BY assessed_at"""
    )


def migrate_to_v16(db: sqlite3.Connection) -> None:
    """停止の種類に judge（判定器の停止）を加える。

    SQLite は CHECK 制約を書き換えられないので、表を作り直して行をすべて写す。
    """
    db.execute("DROP INDEX outages_by_start")
    db.execute("DROP INDEX outages_open")
    db.execute("ALTER TABLE outages RENAME TO outages_before_v16")
    db.execute(
        """CREATE TABLE outages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('monitor', 'camera', 'judge')),
            started_at TEXT NOT NULL,
            notified_at TEXT,
            ended_at TEXT
        )"""
    )
    db.execute(
        """INSERT INTO outages (id, kind, started_at, notified_at, ended_at)
        SELECT id, kind, started_at, notified_at, ended_at FROM outages_before_v16"""
    )
    db.execute("DROP TABLE outages_before_v16")
    db.execute("CREATE INDEX outages_by_start ON outages(started_at)")
    db.execute("CREATE UNIQUE INDEX outages_open ON outages(kind) WHERE ended_at IS NULL")


def migrate_to_v17(db: sqlite3.Connection) -> None:
    """判定器の呼び出しごとのトークン数（CLI が出すときだけ。出さなければ NULL）。

    input_tokens はキャッシュから読んだ分（cached_tokens）を含む入力の合計。
    """
    for column in ("input_tokens", "cached_tokens", "output_tokens"):
        db.execute(f"ALTER TABLE judge_runs ADD COLUMN {column} INTEGER")


def migrate_to_v18(db: sqlite3.Connection) -> None:
    """排泄候補の回収状態、再侵入、通知の送信予約を追加する。"""
    db.execute(
        """CREATE TABLE reentry_waste (
            event_id TEXT PRIMARY KEY REFERENCES events(id),
            configuration_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('unknown', 'uncollected', 'collected')),
            collected_at TEXT,
            position_x REAL,
            position_y REAL,
            current_risk TEXT,
            stool TEXT,
            note TEXT NOT NULL DEFAULT ''
        )"""
    )
    db.execute(
        """CREATE TABLE reentry_visits (
            id INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES reentry_waste(event_id),
            started_at TEXT NOT NULL,
            ended_at TEXT,
            risk TEXT NOT NULL,
            distance REAL,
            clip_id TEXT,
            high_clip_id TEXT,
            interrupted INTEGER NOT NULL DEFAULT 0,
            correct INTEGER,
            stepped INTEGER,
            note TEXT NOT NULL DEFAULT ''
        )"""
    )
    db.execute(
        """CREATE VIEW pending_waste AS
           SELECT e.id, e.timestamp, w.configuration_id, w.position_x, w.position_y, w.current_risk
           FROM reentry_waste w JOIN events e ON e.id=w.event_id
           LEFT JOIN excretion_labels l ON l.event_id=e.id
           WHERE w.status!='collected' AND e.feedback IS NOT 0
             AND l.excretion_type IS NOT 'urine' AND e.location IS NOT 'outside'"""
    )
    db.execute("CREATE INDEX reentry_visits_by_event ON reentry_visits(event_id, started_at)")
    db.execute(
        """CREATE TABLE reentry_notices (
            event_id TEXT NOT NULL REFERENCES reentry_waste(event_id),
            kind TEXT NOT NULL,
            PRIMARY KEY (event_id, kind)
        )"""
    )
    db.execute(
        """CREATE TABLE reentry_configurations (
            id TEXT PRIMARY KEY,
            installed_at TEXT NOT NULL,
            description TEXT NOT NULL,
            layout TEXT NOT NULL
        )"""
    )


def migrate_to_v19(db: sqlite3.Connection) -> None:
    """連続観測が確認できた最後の時刻を保存する。"""
    db.execute("ALTER TABLE reentry_waste ADD COLUMN observed_until TEXT")


def migrate_to_v20(db: sqlite3.Connection) -> None:
    """通知の送信状態を追加する。以前の送信予約は結果不明として扱う。"""
    db.execute("ALTER TABLE reentry_notices ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'")
    db.execute("ALTER TABLE reentry_notices ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1")
    db.execute("ALTER TABLE reentry_notices ADD COLUMN occurred_at TEXT")
    db.execute("ALTER TABLE reentry_notices ADD COLUMN clip_id TEXT")
    db.execute("ALTER TABLE reentry_notices ADD COLUMN next_attempt_at TEXT")
    db.execute(
        "CREATE INDEX reentry_notices_due ON reentry_notices (next_attempt_at) WHERE status='retry'"
    )


def migrate_to_v21(db: sqlite3.Connection) -> None:
    """再侵入の足元の観測回数と品質を追加する。以前の訪問は標本数 0（不明）とする。"""
    db.execute("ALTER TABLE reentry_visits ADD COLUMN foot_samples INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE reentry_visits ADD COLUMN low_foot_samples INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE reentry_visits ADD COLUMN high_foot_quality TEXT")


def migrate_to_v22(db: sqlite3.Connection) -> None:
    """動画の自動判定に、前後の差分の指標を追加する。以前の判定は未計測（NULL）とする。"""
    db.execute("ALTER TABLE video_assessments ADD COLUMN endpoint_change REAL")


def migrate_to_v23(db: sqlite3.Connection) -> None:
    """確認待ちから外した動画を記録する。以前の動画はどれも外していないものとする。"""
    db.execute(
        """CREATE TABLE review_dismissals (
            clip_id TEXT PRIMARY KEY REFERENCES clips(id),
            dismissed_at TEXT NOT NULL
        )"""
    )


def migrate_to_v24(db: sqlite3.Connection) -> None:
    """給餌の予定、予定とのひも付け、日ごとの通知の印を追加する。"""
    db.execute(
        """CREATE TABLE feeding_plans (
            id TEXT PRIMARY KEY, clock TEXT NOT NULL, changed_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))
        )"""
    )
    db.execute("CREATE UNIQUE INDEX feeding_plans_clock ON feeding_plans(clock) WHERE active=1")
    db.execute("ALTER TABLE care_entries ADD COLUMN plan_id TEXT REFERENCES feeding_plans(id)")
    db.execute("ALTER TABLE care_entries ADD COLUMN plan_day TEXT")
    db.execute("CREATE UNIQUE INDEX care_plan_day ON care_entries(plan_id, plan_day)")
    db.execute(
        """CREATE TABLE feeding_reminders (
            plan_id TEXT NOT NULL REFERENCES feeding_plans(id), day TEXT NOT NULL,
            recipient TEXT NOT NULL, next_attempt_at TEXT,
            PRIMARY KEY (plan_id, day, recipient)
        )"""
    )
    db.execute("ALTER TABLE users ADD COLUMN push_feeding INTEGER NOT NULL DEFAULT 0")


def migrate_to_v25(db: sqlite3.Connection) -> None:
    """映像上の便の確認・片付けと、それに基づく通知の状態を追加する。"""
    for column in ("stool_seen_at", "stool_clear_at", "stool_notified_at", "notice_label"):
        db.execute(f"ALTER TABLE video_assessments ADD COLUMN {column} TEXT")
    db.execute(
        "ALTER TABLE video_assessments ADD COLUMN stool_notice_claimed INTEGER NOT NULL DEFAULT 0"
    )


def migrate_to_v26(db: sqlite3.Connection) -> None:
    """削除したお世話の記録を残しつつ、同じ給餌予定に再記録できるようにする。"""
    db.execute("ALTER TABLE care_entries ADD COLUMN deleted_at TEXT")
    db.execute("DROP INDEX care_plan_day")
    db.execute(
        "CREATE UNIQUE INDEX care_plan_day ON care_entries(plan_id, plan_day) "
        "WHERE deleted_at IS NULL"
    )


def migrate_to_v27(db: sqlite3.Connection) -> None:
    """犬の出入りの動画に、トレー付近で観測できた滞在時間を残す。"""
    db.execute("ALTER TABLE clips ADD COLUMN tray_stay_seconds REAL")


def migrate_to_v28(db: sqlite3.Connection) -> None:
    """犬の睡眠の記録（分ごとの状態と、時ごとの検出の統計）を追加する。"""
    db.execute(
        """CREATE TABLE sleep_minutes (
            minute TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            observed_seconds REAL NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE sleep_detection_hours (
            hour TEXT PRIMARY KEY,
            present_seconds REAL NOT NULL,
            detected_seconds REAL NOT NULL
        )"""
    )


def migrate_to_v29(db: sqlite3.Connection) -> None:
    """判定器の呼び出しに失敗した種類と、番号などの短い詳細（成功とこれまでの失敗は NULL）。"""
    for column in ("failure", "failure_detail"):
        db.execute(f"ALTER TABLE judge_runs ADD COLUMN {column} TEXT")


def migrate_to_v30(db: sqlite3.Connection) -> None:
    """判定の前のふるい分けの指標を、動画ごとに記録する表を追加する（判定には使わない）。"""
    db.execute(
        """CREATE TABLE clip_screenings (
            clip_id TEXT PRIMARY KEY REFERENCES clips(id),
            version INTEGER NOT NULL,
            screened_at TEXT NOT NULL,
            sampled INTEGER NOT NULL,
            tray_frames INTEGER NOT NULL,
            weak_tray_frames INTEGER NOT NULL,
            tray_seconds REAL NOT NULL,
            weak_tray_seconds REAL NOT NULL,
            unaligned INTEGER NOT NULL,
            located INTEGER NOT NULL,
            net_turning REAL NOT NULL,
            path_length REAL NOT NULL,
            stop_seconds REAL NOT NULL
        )"""
    )


def _allow_advice_purposes(db: sqlite3.Connection) -> None:
    """判定器の用途に advice（ごはんの相談）と food_lookup（フードのカロリー探し）を加える。

    SQLite は CHECK 制約を書き換えられないので、表を作り直して行をすべて写す。
    """
    db.execute("DROP INDEX judge_runs_by_time")
    db.execute("ALTER TABLE judge_runs RENAME TO judge_runs_before_v31")
    db.execute(
        """CREATE TABLE judge_runs (
        id INTEGER PRIMARY KEY,
        provider TEXT NOT NULL,
        model TEXT,
        purpose TEXT NOT NULL
            CHECK(purpose IN ('review', 'benchmark', 'advice', 'food_lookup')),
        ran_at TEXT NOT NULL,
        failed INTEGER NOT NULL CHECK(failed IN (0, 1)),
        input_tokens INTEGER,
        cached_tokens INTEGER,
        output_tokens INTEGER,
        failure TEXT,
        failure_detail TEXT)"""
    )
    db.execute(
        """INSERT INTO judge_runs (id, provider, model, purpose, ran_at, failed,
            input_tokens, cached_tokens, output_tokens, failure, failure_detail)
        SELECT id, provider, model, purpose, ran_at, failed,
            input_tokens, cached_tokens, output_tokens, failure, failure_detail
        FROM judge_runs_before_v31"""
    )
    db.execute("DROP TABLE judge_runs_before_v31")
    db.execute("CREATE INDEX judge_runs_by_time ON judge_runs(ran_at)")


def migrate_to_v31(db: sqlite3.Connection) -> None:
    """ごはんの量の目安と、家族で共有する相談のための列と表を追加する。"""
    for column in (
        "breed TEXT",
        "size_class TEXT CHECK(size_class IN ('toy', 'small', 'medium', 'large', 'giant'))",
        "adult_grams INTEGER CHECK(adult_grams > 0)",
        "neutered INTEGER CHECK(neutered IN (0, 1))",
        "food_id TEXT",
    ):
        db.execute(f"ALTER TABLE pet_profile ADD COLUMN {column}")
    db.execute(
        """CREATE TABLE foods (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            kcal_dk INTEGER NOT NULL CHECK(kcal_dk BETWEEN 300 AND 7000),
            source_url TEXT NOT NULL DEFAULT '',
            created_by TEXT,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )"""
    )
    db.execute(
        """CREATE TABLE food_lookups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'done', 'failed')),
            product_name TEXT NOT NULL DEFAULT '',
            maker TEXT NOT NULL DEFAULT '',
            kcal_dk INTEGER,
            source_url TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            failure TEXT,
            requested_by TEXT,
            requested_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE advice_threads (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            request_key TEXT UNIQUE
        )"""
    )
    db.execute(
        """CREATE TABLE advice_messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES advice_threads(id),
            seq INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            author TEXT,
            body TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'done', 'failed')),
            failure TEXT,
            basis TEXT NOT NULL DEFAULT '',
            provider TEXT,
            model TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            answered_at TEXT,
            UNIQUE(thread_id, seq)
        )"""
    )
    db.execute("CREATE INDEX advice_messages_status ON advice_messages(status)")
    db.execute("CREATE INDEX food_lookups_status ON food_lookups(status)")
    _allow_advice_purposes(db)


def migrate_to_v32(db: sqlite3.Connection) -> None:
    """確定した記録を開始の時刻で探す索引を足す（ホーム・お世話・相談が期間で読む）。"""
    # 式は Store.RECORD_START と同じにする。違うと SQLite は索引を使わない。
    db.execute(
        "CREATE INDEX events_records_by_start ON events"
        "(julianday(COALESCE(NULLIF(actual_start,''),timestamp))) WHERE feedback=1"
    )


def migrate_to_v33(db: sqlite3.Connection) -> None:
    """シートの汚れの積み重なり（交換どきの目安）を記録する表を追加する。"""
    db.execute(
        """CREATE TABLE sheet_wear (
            id INTEGER PRIMARY KEY,
            at TEXT NOT NULL,
            scene_signature TEXT,
            kind TEXT NOT NULL CHECK(kind IN ('event', 'settled', 'reset', 'notified')),
            ratio REAL CHECK(ratio BETWEEN 0 AND 1),
            source TEXT
        )"""
    )


def migrate_to_v34(db: sqlite3.Connection) -> None:
    """床に新しく現れた物の候補（実験的）と、人のラベルを記録する表を追加する。"""
    db.execute(
        """CREATE TABLE floor_objects (
            id TEXT PRIMARY KEY,
            detected_at TEXT NOT NULL,
            x1 REAL NOT NULL, y1 REAL NOT NULL, x2 REAL NOT NULL, y2 REAL NOT NULL,
            area_ratio REAL NOT NULL,
            label TEXT CHECK(label IN ('foreign', 'vomit', 'diarrhea', 'toy', 'false')),
            labeled_at TEXT,
            labeled_by TEXT,
            notified_at TEXT
        )"""
    )
    db.execute("CREATE INDEX floor_objects_by_time ON floor_objects(detected_at)")


def migrate_to_v35(db: sqlite3.Connection) -> None:
    """いつもと違う日が続いたことを知らせた記録（同じ日の同じ指標を繰り返し知らせない）。"""
    db.execute(
        """CREATE TABLE trend_notices (
            day TEXT NOT NULL,
            metric TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('high', 'low')),
            notified_at TEXT NOT NULL,
            PRIMARY KEY(day, metric)
        )"""
    )


def migrate_to_v36(db: sqlite3.Connection) -> None:
    """犬がサークルにいなかった区間と、家族の「外出中」の切り替えを記録する表を追加する。"""
    db.execute(
        """CREATE TABLE absences (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            away INTEGER NOT NULL DEFAULT 0 CHECK(away IN (0, 1)),
            interrupted INTEGER NOT NULL DEFAULT 0 CHECK(interrupted IN (0, 1)),
            label TEXT CHECK(label IN ('walk', 'out', 'escape', 'false')),
            labeled_at TEXT,
            labeled_by TEXT,
            notified_at TEXT
        )"""
    )
    db.execute("CREATE INDEX absences_by_start ON absences(started_at)")
    db.execute(
        """CREATE TABLE away_mode (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            away_since TEXT,
            changed_by TEXT NOT NULL DEFAULT ''
        )"""
    )


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
    migrate_to_v11,
    migrate_to_v12,
    migrate_to_v13,
    migrate_to_v14,
    migrate_to_v15,
    migrate_to_v16,
    migrate_to_v17,
    migrate_to_v18,
    migrate_to_v19,
    migrate_to_v20,
    migrate_to_v21,
    migrate_to_v22,
    migrate_to_v23,
    migrate_to_v24,
    migrate_to_v25,
    migrate_to_v26,
    migrate_to_v27,
    migrate_to_v28,
    migrate_to_v29,
    migrate_to_v30,
    migrate_to_v31,
    migrate_to_v32,
    migrate_to_v33,
    migrate_to_v34,
    migrate_to_v35,
    migrate_to_v36,
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
        with self.connect() as db:
            if schema_version(db) != LATEST_VERSION:
                migrate(db)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """正常終了なら確定し、例外なら取り消す。どちらの場合も接続を閉じる。"""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            yield db


def events_database(root: str | Path) -> Database:
    """データの保存先の events.sqlite3。保存先のディレクトリがなければ作る。"""
    Path(root).mkdir(parents=True, exist_ok=True)
    return Database(Path(root) / "events.sqlite3")
