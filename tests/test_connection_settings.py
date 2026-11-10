""".envの接続情報をCLIから利用し、既存の環境変数で上書きできることを検証する。"""

import json
import os
from contextlib import nullcontext
from types import SimpleNamespace

from poop_detector.main import main


def test_check_camera_reads_literal_rtsp_from_dotenv_and_honors_environment(tmp_path, monkeypatch):
    calls = []

    def camera(source, *args):
        calls.append(source)
        return nullcontext(SimpleNamespace(read=lambda: SimpleNamespace(shape=(20, 30))))

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POOP_RTSP_URL", raising=False)
    monkeypatch.setattr("poop_detector.main.Camera", camera)
    url = "rtsp://user:${literal}@camera.invalid/live#fragment"
    (tmp_path / ".env").write_text(f"POOP_RTSP_URL='{url}'\n")
    assert main(["check-camera"]) == 0
    assert calls == [url]
    assert "POOP_RTSP_URL" not in os.environ

    monkeypatch.setenv("POOP_RTSP_URL", "rtsp://override.invalid/live")
    assert main(["check-camera"]) == 0
    assert calls[-1] == "rtsp://override.invalid/live"
    monkeypatch.setenv("POOP_RTSP_URL", "")
    assert main(["check-camera"]) == 2
    assert len(calls) == 2


def test_notification_reads_both_topics_from_dotenv(tmp_path, monkeypatch):
    sent = []

    def opener(request, *, timeout):
        sent.append(json.loads(request.data)["topic"])
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.chdir(tmp_path)
    for key in ("POOP_RTSP_URL", "POOP_NTFY_TOPIC", "POOP_NTFY_EARLY_TOPIC"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("POOP_NTFY_TOPIC=family\nPOOP_NTFY_EARLY_TOPIC='watch'\n")
    monkeypatch.setattr("poop_detector.main.platform.system", lambda: "Linux")
    monkeypatch.setattr("poop_detector.notifications.urlopen", opener)
    assert main(["test-notification"]) == 0
    assert main(["test-notification", "--early-alert"]) == 0
    assert sent == ["family", "watch"]
