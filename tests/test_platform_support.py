"""対応 OS の判定、画面の有無の判定、OS に依存しない表記を検証する。"""

import http.client
import logging
import threading

import pytest
from support import session_cookie

from poop_detector.main import main
from poop_detector.platform_support import (
    UnsupportedPlatformError,
    ensure_supported,
    has_display,
)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_macos_and_linux_are_supported(platform):
    ensure_supported(platform)


@pytest.mark.parametrize("platform", ["win32", "cygwin"])
def test_other_platforms_are_rejected_with_reason(platform):
    with pytest.raises(UnsupportedPlatformError, match="macOS と Linux"):
        ensure_supported(platform)


def test_display_is_assumed_on_macos():
    assert has_display("darwin", {})


@pytest.mark.parametrize(
    ("environ", "expected"),
    [({}, False), ({"DISPLAY": ":0"}, True), ({"WAYLAND_DISPLAY": "wayland-0"}, True)],
)
def test_linux_needs_display_or_wayland(environ, expected):
    assert has_display("linux", environ) is expected


@pytest.mark.parametrize("command", ["setup-roi", "setup-activity-map"])
def test_gui_setup_without_display_explains_how_to_prepare(command, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://camera.invalid/live")
    monkeypatch.setattr("poop_detector.commands.setup.has_display", lambda: False)

    def camera(*args, **kwargs):
        raise AssertionError("画面がない場合はカメラに接続しない")

    monkeypatch.setattr("poop_detector.commands.context.Camera", camera)
    assert main([command]) == 2
    assert "画面" in capsys.readouterr().err


def test_linux_without_ntfy_warns_that_no_notification_is_sent(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    for key in ("POOP_NTFY_TOPIC", "POOP_NTFY_EARLY_TOPIC"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("poop_detector.commands.context.platform.system", lambda: "Linux")
    with caplog.at_level(logging.WARNING):
        assert main(["test-notification"]) == 2
    assert "POOP_NTFY_TOPIC" in caplog.text


def test_gui_header_does_not_assume_mac(tmp_path):
    from poop_detector.gui.server import make_server

    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(*server.server_address)
        conn.request(
            "GET",
            "/",
            headers={
                "Host": f"127.0.0.1:{server.server_port}",
                **session_cookie(server),
            },
        )
        page = conn.getresponse().read().decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert "Mac" not in page
    assert "このマシン内で保存" in page


def test_any_submodule_import_silences_opencv_logs_first():
    """RTSP の認証情報がログに出ないよう、どのモジュールから読み込んでも先に抑制する。"""
    import os
    import subprocess
    import sys

    code = (
        "import os, sys\n"
        "import poop_detector.capture.camera\n"
        "print(os.environ['OPENCV_LOG_LEVEL'], os.environ['OPENCV_FFMPEG_LOGLEVEL'])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={key: value for key, value in os.environ.items() if not key.startswith("OPENCV_")},
    )
    assert result.stdout.split() == ["SILENT", "-8"]
