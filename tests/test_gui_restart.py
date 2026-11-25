"""管理画面からの再起動と、設定の再読み込みを確認する。"""

import json
import threading

from support import login, request, submit

from poop_detector.gui.server import make_server
from poop_detector.main import main


def test_restart_requires_supported_launch(gui):
    server, _, _ = gui
    assert "この起動方法では".encode() in request(server, "GET", "/manage")[1]
    assert submit(server, "/manage/restart", {"confirm": "yes"})[0] == 409


def test_restart_requires_confirmation_and_accepts_only_once(tmp_path):
    from poop_detector.gui.restart import Restart

    restart = Restart("gui")
    with make_server(tmp_path, 0, restart=restart) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            assert request(server, "GET", "/assets/restart.js")[0] == 200
            path = "/manage/restart"
            assert request(server, "GET", path, auth=False)[0] == 403
            member = login(server, role="member")
            assert request(server, "GET", path, headers=member)[0] == 403
            assert request(server, "POST", path, "confirm=yes")[0] == 403
            status = json.loads(request(server, "GET", path)[1])
            fields = {"generation": status["generation"]}
            assert submit(server, path, fields)[0] == 400
            assert not restart.requested.is_set()
            assert submit(server, path, {**fields, "confirm": "yes"})[0] == 202
            assert restart.requested.is_set()
            assert submit(server, path, {**fields, "confirm": "yes"})[0] == 409
        finally:
            server.shutdown()
            worker.join()


def test_cli_reloads_config_after_shutdown(tmp_path, monkeypatch):
    from poop_detector.commands import records

    config = tmp_path / "config.yaml"
    config.write_text("pet_name: 初回\n")
    names = []

    def gui(args, context):
        names.append(context.config.pet_name)
        if len(names) == 1:
            context.restart.requested.set()
            config.write_text("pet_name: 再起動後\n")
        return 0

    monkeypatch.setattr(records, "gui", gui)
    assert main(["--config", str(config), "gui", "--no-browser"]) == 0
    assert names == ["初回", "再起動後"]
