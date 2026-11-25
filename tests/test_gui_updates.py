"""PyPI の安定版表示と、管理 GUI の更新要求を確認する。"""

import json
from importlib.metadata import version

from support import request, submit


def test_installed_version_and_new_stable_release(gui, monkeypatch):
    from poop_detector.gui import updates

    server, _, _ = gui
    monkeypatch.setattr(updates, "latest_stable", lambda: "999.0.0")
    html = request(server, "GET", "/manage")[1].decode()
    assert version("poop-detector") in html
    status, body = request(server, "GET", "/manage/update/status")
    assert status == 200
    assert json.loads(body)["latest"] == "999.0.0"
    assert json.loads(body)["available"]


def test_install_requires_supported_launch(gui, monkeypatch):
    from poop_detector.gui import updates

    server, _, _ = gui
    monkeypatch.setattr(updates, "latest_stable", lambda: "999.0.0")
    assert submit(server, "/manage/update", {"confirm": "yes", "target": "999.0.0"})[0] == 409


def test_latest_stable_ignores_prereleases_yanked_and_invalid_versions():
    from poop_detector.gui.updates import stable_version

    wheel = {"packagetype": "bdist_wheel", "yanked": False}
    assert (
        stable_version(
            {
                "2.9.0": [wheel],
                "2.10.0": [wheel],
                "3.0.0rc1": [wheel],
                "7.0.0": [{**wheel, "yanked": True}],
                "8.0.0": [{"packagetype": "sdist"}],
                "invalid": [wheel],
            }
        )
        == "2.10.0"
    )


def test_update_stages_once_and_requests_graceful_restart(gui, monkeypatch, tmp_path):
    import threading

    from poop_detector.gui import updates
    from poop_detector.gui.restart import Restart

    server, store, event_id = gui
    upgrade = updates.Upgrade(tmp_path / "versions", "/usr/bin/uv")
    restart = Restart("gui", upgrade)
    monkeypatch.setattr(type(server.app), "restart", property(lambda self: restart))
    monkeypatch.setattr(updates, "latest_stable", lambda: "999.0.0")
    entered = threading.Event()
    release = threading.Event()
    next_python = tmp_path / "release-test" / "bin" / "python"

    def prepare(target):
        assert target == "999.0.0"
        entered.set()
        assert release.wait(3)
        return next_python

    monkeypatch.setattr(upgrade, "prepare", prepare)
    fields = {"generation": restart.generation, "target": "999.0.0", "confirm": "yes"}
    try:
        assert submit(server, "/manage/update", {**fields, "target": "998.0.0"})[0] == 400
        assert submit(server, "/manage/update", fields)[0] == 202
        assert entered.wait(2)
        assert submit(server, "/manage/update", fields)[0] == 409
        assert submit(server, "/manage/restart", fields)[0] == 409
        assert (
            json.loads(request(server, "GET", "/manage/update/status")[1])["phase"] == "preparing"
        )
        assert store.list()[0]["id"] == event_id
    finally:
        release.set()
    assert restart.requested.wait(2)
    assert upgrade.python == next_python
    assert not upgrade.marker.exists()


def test_update_failure_leaves_running_version_available(monkeypatch, tmp_path, caplog):
    from poop_detector.gui import updates
    from poop_detector.gui.restart import Restart

    upgrade = updates.Upgrade(tmp_path / "versions", "/usr/bin/uv")
    restart = Restart("gui", upgrade)

    def fail(_target):
        raise OSError("取得失敗")

    monkeypatch.setattr(upgrade, "prepare", fail)
    restart._prepare(upgrade, "999.0.0")
    assert upgrade.phase == "failed"
    assert not restart.requested.is_set()
    assert "更新準備に失敗" in caplog.text


def test_update_exec_failure_restores_previous_environment(tmp_path, monkeypatch):
    import pytest

    from poop_detector.gui import updates

    upgrade = updates.Upgrade(tmp_path, "/usr/bin/uv")
    old = tmp_path / "release-old"
    old.mkdir()
    (old / "pyvenv.cfg").touch()
    upgrade.marker.write_text("release-old\n")
    python = tmp_path / "release-new" / "bin" / "python"

    def fail(executable, argv):
        assert executable == python
        assert argv == ["--config", "original.yaml", "gui", "--no-browser"]
        assert upgrade.marker.read_text() == "release-new\n"
        raise OSError("起動失敗")

    monkeypatch.setattr(updates, "exec_python", fail)
    with pytest.raises(OSError, match="起動失敗"):
        upgrade.activate(python, ["--config", "original.yaml", "gui", "--no-browser"])
    assert upgrade.active_python() == old / "bin" / "python"
    assert (tmp_path / "previous").read_text() == "release-old\n"


def test_latest_stable_cache_and_network_failure(monkeypatch):
    import io

    from poop_detector.gui import updates

    wheel = {"packagetype": "bdist_wheel", "yanked": False}
    requests = []

    def response(url, *, timeout):
        requests.append((url, timeout))
        if len(requests) > 1:
            raise OSError("オフライン")
        return io.BytesIO(json.dumps({"releases": {"9.9.9": [wheel]}}).encode())

    monkeypatch.setattr(updates, "urlopen", response)
    monkeypatch.setattr(updates, "monotonic", lambda: 1)
    updates._check_latest.cache_clear()
    try:
        assert updates.latest_stable() == "9.9.9"
        assert updates.latest_stable() == "9.9.9"
        assert requests == [(updates.PYPI_JSON, 4)]
        monkeypatch.setattr(updates, "monotonic", lambda: 301)
        assert updates.latest_stable() is None
    finally:
        updates._check_latest.cache_clear()


def test_prepare_uses_a_separate_environment_and_cleans_up_on_failure(tmp_path, monkeypatch):
    import subprocess

    import pytest

    from poop_detector.gui import updates

    upgrade = updates.Upgrade(tmp_path / "staging", "/usr/bin/uv")
    commands = []

    def run(command, *, env, cwd, **_options):
        commands.append(command)
        assert cwd.parent == upgrade.directory
        assert "POOP_RTSP_URL" not in env
        assert "UV_INDEX" not in env
        return subprocess.CompletedProcess(command, 0, stdout=b"9.9.9\n")

    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://test.invalid")
    monkeypatch.setenv("UV_INDEX", "https://test.invalid/")
    monkeypatch.setattr(updates.subprocess, "run", run)
    python = upgrade.prepare("9.9.9")
    assert commands[0][2] == "venv"
    assert "--default-index" in commands[1]
    assert updates.PYPI_INDEX in commands[1]
    assert commands[1][-1].endswith("==9.9.9")
    assert commands[2] == [str(python), "-I", "-m", "poop_detector.main", "--version"]
    assert not upgrade.marker.exists()
    assert upgrade.active_python() is None
    with pytest.raises(ValueError, match="CLI バージョン"):
        upgrade.prepare("9.9.8")
    assert list(upgrade.directory.iterdir()) == [python.parent.parent]


def test_cli_uses_prepared_version_and_reopens_after_exec_failure(tmp_path, monkeypatch):
    from poop_detector import main as cli
    from poop_detector.commands import records

    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {tmp_path / 'records'}\npet_name: 変えない\n")
    update_dir = tmp_path / "versions"
    update_dir.mkdir()
    old = update_dir / "release-old"
    old.mkdir()
    (old / "pyvenv.cfg").touch()
    marker = update_dir / "active"
    marker.write_text("release-old\n")
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/uv")
    calls = []

    def launch(python, args):
        calls.append((python, args.copy()))
        raise OSError("起動できません")

    monkeypatch.setattr(cli, "exec_python", launch)
    args = ["--config", str(config), "--update-dir", str(update_dir), "gui"]
    assert cli.main(args) == 2
    assert calls == [(old / "bin" / "python", args)]

    marker.write_text("")
    seen = []

    def serve(args, context):
        seen.append((context.config.pet_name, context.restart.upgrade.phase, args.no_browser))
        if len(seen) == 1:
            context.restart.upgrade.python = tmp_path / "release-new" / "bin" / "python"
            context.restart.requested.set()
        return 0

    monkeypatch.setattr(records, "gui", serve)
    monkeypatch.setattr("poop_detector.gui.updates.exec_python", launch)
    assert cli.main(args) == 0
    assert seen == [("変えない", "idle", False), ("変えない", "failed", True)]
    assert calls[-1][1] == [*args, "--no-browser"]
    assert marker.read_text() == ""
