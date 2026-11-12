"""backup・restore サブコマンド（#90）を検証する。"""

import zipfile

import pytest

from poop_detector.gui.accounts import Accounts
from poop_detector.main import main
from poop_detector.records.database import events_database
from poop_detector.records.events import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "data"
    Store(root).add_excretion("feces", "2026-11-01T08:00:00+09:00", note="元")
    (root / "roi.json").write_text("{}")
    (root / "clips").mkdir()
    (root / "clips" / "one.mp4").write_bytes(b"mp4")
    (tmp_path / "config.yaml").write_text(f"data_dir: {root}\n")
    return tmp_path, root


def run(*args):
    return main(["--config", "config.yaml", *args])


def actions(root):
    return [entry.action for entry in Accounts(events_database(root)).audit.recent()]


def test_backup_writes_a_zip_and_never_overwrites(env, capsys):
    path, root = env
    assert run("backup", "out.zip") == 0
    assert "clips/one.mp4" not in zipfile.ZipFile(path / "out.zip").namelist()
    assert "out.zip" in capsys.readouterr().out
    assert "backup.create" in actions(root)
    assert run("backup", "out.zip") == 2
    assert "既にあります" in capsys.readouterr().err
    assert run("backup", "with-clips.zip", "--clips") == 0
    assert "clips/one.mp4" in zipfile.ZipFile(path / "with-clips.zip").namelist()


def test_restore_asks_before_replacing_and_keeps_the_original(env, capsys, monkeypatch):
    path, root = env
    assert run("backup", "out.zip") == 0
    Store(root).add_excretion("urine", "2026-11-10T08:00:00+09:00", note="あと")
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert run("restore", "out.zip") == 1
    assert "取りやめました" in capsys.readouterr().out
    assert len(Store(root).records()) == 2

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert run("restore", "out.zip") == 0
    out = capsys.readouterr().out
    assert "data.before-restore-" in out
    assert [r["note"] for r in Store(root).records()] == ["元"]
    kept = [p for p in path.iterdir() if p.name.startswith("data.before-restore-")]
    assert len(kept) == 1
    assert len(Store(kept[0]).records()) == 2
    assert "backup.restore" in actions(root)


def test_restore_with_yes_skips_the_question(env, monkeypatch):
    assert run("backup", "out.zip") == 0

    def refuse(_prompt):
        raise AssertionError("確認を求めないはず")

    monkeypatch.setattr("builtins.input", refuse)
    assert run("restore", "out.zip", "--yes") == 0


def test_restore_rejects_a_file_that_is_not_a_backup(env, capsys):
    path, _ = env
    (path / "bad.zip").write_bytes(b"nope")
    assert run("restore", "bad.zip", "--yes") == 2
    assert "ZIP" in capsys.readouterr().err
