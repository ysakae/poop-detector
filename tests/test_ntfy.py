"""ntfyへの送信内容とLinuxでの通知設定を検証する。"""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from poop_detector.main import main
from poop_detector.notifications import NtfyNotifier


def test_ntfy_sends_only_text_for_the_existing_notification_types():
    sent = []

    def open_request(request, *, timeout):
        assert request.full_url == "https://ntfy.sh"
        assert request.get_method() == "POST"
        assert request.get_header("Content-type") == "application/json"
        assert timeout == 5
        sent.append(json.loads(request.data))
        return nullcontext(SimpleNamespace(status=200))

    notifier = NtfyNotifier("test-family", opener=open_request)
    assert notifier.send()
    assert notifier.send_early_alert()
    assert notifier.send_assessment("urine", location="outside")
    assert not notifier.send_assessment("uncertain")
    assert len(sent) == 3
    assert all(payload.keys() == {"topic", "title", "message"} for payload in sent)
    assert all(payload["topic"] == "test-family" for payload in sent)
    assert "トレー外での排尿の候補" in sent[-1]["message"]


def test_ntfy_handles_network_failure_and_keeps_topic_out_of_logs(caplog):
    def fail(*args, **kwargs):
        raise URLError("private-topic")

    assert not NtfyNotifier("private-topic", opener=fail).send()
    assert "private-topic" not in caplog.text
    with pytest.raises(ValueError, match="POOP_NTFY_TOPIC"):
        NtfyNotifier("https://ntfy.sh/private-topic")


def test_ntfy_sends_only_early_alerts_to_the_separate_topic():
    sent = []

    def open_request(request, *, timeout):
        sent.append(json.loads(request.data))
        return nullcontext(SimpleNamespace(status=200))

    notifier = NtfyNotifier("family", early_topic="watch", opener=open_request)
    assert notifier.send_early_alert()
    assert notifier.send()
    assert notifier.send_assessment("both")
    assert [payload["topic"] for payload in sent] == ["watch", "family", "family"]
    assert "見守り" in sent[0]["title"]
    with pytest.raises(ValueError, match="POOP_NTFY_EARLY_TOPIC"):
        NtfyNotifier("family", early_topic="family")


def test_cli_can_send_a_distinct_test_message_on_linux(tmp_path, monkeypatch, capsys):
    sent = []
    monkeypatch.chdir(tmp_path)

    def open_request(request, *, timeout):
        sent.append(json.loads(request.data))
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setenv("POOP_NTFY_TOPIC", "test-family")
    monkeypatch.delenv("POOP_NTFY_EARLY_TOPIC", raising=False)
    monkeypatch.setattr("poop_detector.main.platform.system", lambda: "Linux")
    monkeypatch.setattr("poop_detector.notifications.urlopen", open_request)
    config = tmp_path / "config.yaml"
    config.write_text("notifications: true\n")
    command = ["--config", str(config), "test-notification"]
    assert main(command) == 0
    assert "テスト" in sent[0]["message"]
    assert "iPhone" in capsys.readouterr().out
    config.write_text("notifications: false\n")
    assert main(command) == 2
    assert len(sent) == 1


def test_cli_can_test_early_topic_without_sending_to_the_main_topic(tmp_path, monkeypatch, capsys):
    sent = []
    monkeypatch.chdir(tmp_path)

    def open_request(request, *, timeout):
        sent.append(json.loads(request.data))
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setenv("POOP_NTFY_TOPIC", "family")
    monkeypatch.delenv("POOP_NTFY_EARLY_TOPIC", raising=False)
    monkeypatch.setattr("poop_detector.notifications.urlopen", open_request)
    command = ["--config", str(tmp_path / "config.yaml"), "test-notification", "--early-alert"]
    assert main(command) == 2
    assert not sent
    assert "POOP_NTFY_EARLY_TOPIC" in capsys.readouterr().err
    monkeypatch.setenv("POOP_NTFY_EARLY_TOPIC", "watch")
    assert main(command) == 0
    assert [payload["topic"] for payload in sent] == ["watch"]
    assert "テスト" in sent[0]["message"]
