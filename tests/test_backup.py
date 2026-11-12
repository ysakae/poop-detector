"""データのバックアップの作成と復元（#90）を検証する。"""

import fcntl
import json
import sqlite3
import zipfile
from datetime import UTC, datetime

import numpy as np
import pytest

from poop_detector.errors import AlreadyRunningError, InvalidInputError
from poop_detector.gui.accounts import Accounts
from poop_detector.records.backup import (
    MANIFEST,
    create_backup,
    inspect_backup,
    restore_backup,
)
from poop_detector.records.database import LATEST_VERSION, events_database
from poop_detector.records.events import Store

NOW = datetime(2026, 11, 12, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def data(tmp_path):
    """記録・登録情報・動画・秘密の値を置いたデータの保存先。"""
    root = tmp_path / "data"
    store = Store(root)
    store.record(np.full((20, 20, 3), 90, np.uint8), 0.1, None, 1)
    store.add_excretion("feces", "2026-11-01T08:00:00+09:00", note="元のメモ")
    accounts = Accounts(events_database(root))
    user = accounts.users.create("管理者", "admin")
    with store.connect() as db:
        db.execute(
            "INSERT INTO gui_sessions (id,user_id,created_at,last_seen) VALUES (?,?,?,?)",
            ("SESSION-SECRET-HASH", user, NOW.isoformat(), NOW.isoformat()),
        )
        db.execute(
            "INSERT INTO push_subscriptions VALUES (?,?,?,?,?)",
            ("https://push.example/PUSH-SECRET", user, "P256DH-SECRET", "AUTH-SECRET", "x"),
        )
    (root / "roi.json").write_text('{"x": 0}')
    (root / "outside-roi.json").write_text('{"x": 1}')
    (root / "baseline").mkdir()
    (root / "baseline" / "empty.png").write_bytes(b"png")
    (root / "baseline" / "empty.json").write_text("{}")
    (root / "tray-scenes" / "ab").mkdir(parents=True)
    (root / "tray-scenes" / "ab" / "roi.json").write_text("{}")
    (root / "activity-layout.json").write_text("{}")
    (root / f"activity-{'a' * 32}.jpg").write_bytes(b"jpg")
    sqlite3.connect(root / "activity.sqlite3").execute("CREATE TABLE t (x)").connection.commit()
    (root / "clips").mkdir()
    (root / "clips" / "one.mp4").write_bytes(b"mp4" * 100)
    (root / ".web-push-vapid.pem").write_text("VAPID-PRIVATE-KEY")
    (root / ".live-preview.jpg").write_bytes(b"live")
    (root / "models" / "dog-yolox").mkdir(parents=True)
    (root / "models" / "dog-yolox" / "model.onnx").write_bytes(b"model")
    return root


def names(path):
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def test_backup_contains_records_and_registrations_but_not_secrets(data, tmp_path):
    target = tmp_path / "backup.zip"
    summary = create_backup(data, target, include_clips=False, now=NOW)
    listed = names(target)
    assert {
        MANIFEST,
        "events.sqlite3",
        "activity.sqlite3",
        "roi.json",
        "outside-roi.json",
        "baseline/empty.png",
        "baseline/empty.json",
        "tray-scenes/ab/roi.json",
        "activity-layout.json",
        f"activity-{'a' * 32}.jpg",
    } <= listed
    assert any(name.startswith("events/") and name.endswith(".jpg") for name in listed)
    assert not any(name.startswith("clips/") for name in listed)
    assert not any(name.startswith("models/") for name in listed)
    assert ".web-push-vapid.pem" not in listed
    assert ".live-preview.jpg" not in listed
    assert summary.clips == 0
    raw = target.read_bytes()
    for secret in (b"SESSION-SECRET-HASH", b"PUSH-SECRET", b"AUTH-SECRET", b"VAPID-PRIVATE"):
        assert secret not in raw
    manifest = json.loads(zipfile.ZipFile(target).read(MANIFEST))
    assert manifest["schema_version"] == LATEST_VERSION
    assert manifest["include_clips"] is False
    assert manifest["created_at"] == NOW.isoformat()


def test_backup_database_keeps_records_and_users_but_drops_sessions(data, tmp_path):
    target = tmp_path / "backup.zip"
    create_backup(data, target, include_clips=False, now=NOW)
    copy = tmp_path / "copy.sqlite3"
    copy.write_bytes(zipfile.ZipFile(target).read("events.sqlite3"))
    db = sqlite3.connect(copy)
    assert db.execute("SELECT note FROM excretion_labels").fetchone() == ("元のメモ",)
    assert db.execute("SELECT name FROM users").fetchone() == ("管理者",)
    for table in ("gui_sessions", "enrollments", "push_subscriptions"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)  # noqa: S608
    db.close()
    # 元のデータベースは変えない
    with sqlite3.connect(data / "events.sqlite3") as original:
        assert original.execute("SELECT COUNT(*) FROM gui_sessions").fetchone() == (1,)


def test_backup_can_include_clips(data, tmp_path):
    target = tmp_path / "backup.zip"
    summary = create_backup(data, target, include_clips=True, now=NOW)
    assert "clips/one.mp4" in names(target)
    assert summary.clips == 1


def test_restore_keeps_the_previous_data_and_brings_back_the_backup(data, tmp_path):
    target = tmp_path / "backup.zip"
    create_backup(data, target, include_clips=True, now=NOW)
    Store(data).add_excretion("urine", "2026-11-10T08:00:00+09:00", note="あとから")
    (data / "roi.json").write_text('{"x": 99}')

    preserved = restore_backup(target, data, now=NOW)

    assert preserved.parent == data.parent
    assert preserved.name == "data.before-restore-20261112-030405"
    assert (preserved / "roi.json").read_text() == '{"x": 99}'
    assert (preserved / ".web-push-vapid.pem").is_file()
    assert (data / "roi.json").read_text() == '{"x": 0}'
    assert (data / "clips" / "one.mp4").is_file()
    notes = [row["note"] for row in Store(data).records()]
    assert notes == ["元のメモ"]
    # 取得し直しが重い犬検出モデルは、元のデータから写す
    assert (data / "models" / "dog-yolox" / "model.onnx").read_bytes() == b"model"
    assert not (data / ".web-push-vapid.pem").exists()


def test_restore_into_a_new_machine_without_data(data, tmp_path):
    target = tmp_path / "backup.zip"
    create_backup(data, target, include_clips=False, now=NOW)
    fresh = tmp_path / "fresh"
    assert restore_backup(target, fresh, now=NOW) is None
    assert (fresh / "roi.json").is_file()


def test_restore_refuses_while_the_monitor_holds_the_lock(data, tmp_path):
    target = tmp_path / "backup.zip"
    create_backup(data, target, include_clips=False, now=NOW)
    with (data / ".capture.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(AlreadyRunningError):
            restore_backup(target, data, now=NOW)
    assert (data / "roi.json").is_file()


def craft(path, entries, manifest=None):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
        if manifest is not None:
            archive.writestr(MANIFEST, json.dumps(manifest))


def test_inspect_rejects_files_that_are_not_backups(tmp_path):
    (tmp_path / "plain.zip").write_bytes(b"not a zip")
    with pytest.raises(InvalidInputError):
        inspect_backup(tmp_path / "plain.zip")
    craft(tmp_path / "nomanifest.zip", {"events.sqlite3": b""})
    with pytest.raises(InvalidInputError):
        inspect_backup(tmp_path / "nomanifest.zip")


def test_inspect_rejects_paths_outside_the_data_and_tampered_files(data, tmp_path):
    target = tmp_path / "backup.zip"
    create_backup(data, target, include_clips=False, now=NOW)
    manifest = json.loads(zipfile.ZipFile(target).read(MANIFEST))

    escaping = {**manifest, "files": {**manifest["files"], "../evil.txt": "0" * 64}}
    craft(tmp_path / "escape.zip", {"../evil.txt": b"x"}, escaping)
    with pytest.raises(InvalidInputError):
        inspect_backup(tmp_path / "escape.zip")

    secret = {**manifest, "files": {**manifest["files"], ".web-push-vapid.pem": "0" * 64}}
    craft(tmp_path / "secret.zip", {".web-push-vapid.pem": b"x"}, secret)
    with pytest.raises(InvalidInputError):
        inspect_backup(tmp_path / "secret.zip")

    with zipfile.ZipFile(target) as source:
        entries = {name: source.read(name) for name in source.namelist() if name != MANIFEST}
    entries["roi.json"] = b'{"x": "changed"}'
    craft(tmp_path / "tampered.zip", entries, manifest)
    with pytest.raises(InvalidInputError):
        restore_backup(tmp_path / "tampered.zip", data, now=NOW)
    assert (data / "roi.json").read_text() == '{"x": 0}'


def test_inspect_rejects_backups_from_a_newer_schema(data, tmp_path):
    target = tmp_path / "backup.zip"
    create_backup(data, target, include_clips=False, now=NOW)
    with zipfile.ZipFile(target) as source:
        entries = {name: source.read(name) for name in source.namelist() if name != MANIFEST}
        manifest = json.loads(source.read(MANIFEST))
    craft(tmp_path / "newer.zip", entries, {**manifest, "schema_version": LATEST_VERSION + 1})
    with pytest.raises(InvalidInputError):
        inspect_backup(tmp_path / "newer.zip")
