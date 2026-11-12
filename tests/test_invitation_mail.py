"""招待メールの SMTP 接続を、実際の配送を行わずに確かめる。"""

import smtplib

import pytest

from poop_detector.gui import invitation_mail


@pytest.mark.parametrize("port", ["587", "465"])
def test_smtp_uses_tls_before_login_and_sends_single_recipient(monkeypatch, port):
    events = []

    class SMTP:
        def __init__(self, host, port, **kwargs):
            events.append(("connect", host, port, "context" in kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def starttls(self, *, context):
            events.append(("tls", context.check_hostname))

        def login(self, username, password):
            events.append(("login", username, password))

        def send_message(self, message):
            events.append(("send", message["From"], message["To"], message.get_content()))

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", SMTP)
    for key, value in zip(
        invitation_mail.SMTP_KEYS,
        ["smtp.example.org", port, "sender@example.org", "login", "secret"],
        strict=True,
    ):
        monkeypatch.setenv(key, value)
    invitation_mail._send("family@example.org", "登録", "期限付きリンク")
    assert events[0] == ("connect", "smtp.example.org", int(port), port == "465")
    if port == "587":
        assert events[1] == ("tls", True)
    assert events[-2:] == [
        ("login", "login", "secret"),
        ("send", "sender@example.org", "family@example.org", "期限付きリンク\n"),
    ]


def test_smtp_errors_do_not_disclose_server_response(monkeypatch):
    monkeypatch.setenv("POOP_SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("POOP_SMTP_FROM", "sender@example.org")
    monkeypatch.setenv("POOP_SMTP_PORT", "587")
    monkeypatch.setenv("POOP_SMTP_USERNAME", "")
    monkeypatch.setenv("POOP_SMTP_PASSWORD", "")

    def failure(*_, **kwargs):
        raise smtplib.SMTPException("private server response")

    monkeypatch.setattr(smtplib, "SMTP", failure)
    with pytest.raises(invitation_mail.MailError, match="SMTP の設定と接続") as error:
        invitation_mail._send("family@example.org", "登録", "期限付きリンク")
    assert "private" not in str(error.value)
