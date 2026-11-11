"""利用者に伝える失敗の種類と、出力から認証情報を除く処理を検証する。

RTSP の URL は認証情報を含むので、どの出力経路（例外の文言、ログ、詳細表示の
トレースバック）にも出ないことを確かめる。
"""

import logging

import pytest

from poop_detector import main as entry
from poop_detector.commands.context import CommandError
from poop_detector.config import ConfigError
from poop_detector.errors import (
    CameraUnavailableError,
    InvalidInputError,
    ReferenceMismatchError,
    SetupRequiredError,
    UserError,
)
from poop_detector.features.tray_tracking import TrayReferenceError
from poop_detector.redaction import RedactingFormatter, install_log_redaction, redact

SECRET_URL = "rtsp://private:secret@camera.invalid:554/live0"


# --- 伏せ字 ---


@pytest.mark.parametrize(
    "text",
    [
        SECRET_URL,
        f"接続できません: {SECRET_URL}",
        "rtsps://private:secret@camera.invalid/live",
        "RTSP://private:secret@CAMERA.invalid/live",
        f"'{SECRET_URL}'",
        f"url={SECRET_URL}&x=1",
    ],
)
def test_redact_hides_camera_urls(text):
    hidden = redact(text)
    assert "secret" not in hidden
    assert "private" not in hidden
    assert "camera.invalid" not in hidden
    assert "rtsp" in hidden.lower()


def test_redact_hides_credentials_in_other_urls():
    hidden = redact("https://user:token@ntfy.invalid/topic")
    assert "token" not in hidden
    assert "user" not in hidden
    assert "ntfy.invalid/topic" in hidden


def test_redact_keeps_ordinary_text():
    text = "ROIが変わっています。https://ntfy.sh を使います"
    assert redact(text) == text


def test_formatter_hides_urls_in_messages_and_tracebacks():
    formatter = RedactingFormatter("%(message)s")
    try:
        raise OSError(f"open failed: {SECRET_URL}")
    except OSError:
        import sys

        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "接続先 %s", (SECRET_URL,), sys.exc_info()
        )
    text = formatter.format(record)
    assert "Traceback" in text
    assert "secret" not in text


def test_log_records_are_redacted_for_every_handler(caplog):
    install_log_redaction()
    install_log_redaction()  # 2回呼んでも二重にならない
    logging.getLogger("poop_detector.test").warning("接続先 %s", SECRET_URL)
    assert "secret" not in caplog.text
    assert "接続先 rtsp://" in caplog.text


# --- 例外の種類 ---


@pytest.mark.parametrize(
    "error",
    [
        CommandError,
        ConfigError,
        TrayReferenceError,
        SetupRequiredError,
        ReferenceMismatchError,
        CameraUnavailableError,
        InvalidInputError,
    ],
)
def test_package_errors_are_user_errors(error):
    assert issubclass(error, UserError)


@pytest.mark.parametrize("error", [SetupRequiredError, ReferenceMismatchError, InvalidInputError])
def test_input_errors_stay_value_errors_for_existing_callers(error):
    """GUI などは ValueError で利用者向けの文言を受け取るので、互換を保つ。"""
    assert issubclass(error, ValueError)


# --- main() の出力 ---


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(f"data_dir: {tmp_path / 'data'}\n")
    # --verbose が変えるログの詳細度を、テストの後に戻す。
    root = logging.getLogger()
    monkeypatch.setattr(root, "level", root.level)

    def run(failure, *options):
        def handler(_args, _context):
            raise failure

        monkeypatch.setitem(entry.HANDLERS, "events", handler)
        return entry.main(["--config", "config.yaml", *options, "events"])

    return run


def test_user_error_is_shown_as_written_but_without_urls(cli, capsys):
    assert cli(SetupRequiredError(f"先に setup-roi を実行してください（{SECRET_URL}）")) == 2
    err = capsys.readouterr().err
    assert "先に setup-roi を実行してください" in err
    assert "secret" not in err


def test_external_error_hides_its_text_but_names_its_kind(cli, capsys):
    assert cli(OSError(f"cannot open {SECRET_URL}")) == 2
    err = capsys.readouterr().err
    assert "secret" not in err
    assert "OSError" in err
    assert "--verbose" in err
    assert "Traceback" not in err


def test_verbose_shows_a_redacted_traceback(cli, capsys):
    assert cli(OSError(f"cannot open {SECRET_URL}"), "--verbose") == 2
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "cannot open rtsp://" in err
    assert "secret" not in err


def test_programming_errors_are_not_reported_as_input_errors(cli, capsys):
    """TypeError などの誤りは、入力の誤りとは別の終了コードと文言で伝える。"""
    assert cli(TypeError(f"bad call {SECRET_URL}")) == 1
    err = capsys.readouterr().err
    assert "TypeError" in err
    assert "不具合" in err
    assert "入力値を確認" not in err
    assert "secret" not in err


def test_verbose_flag_enables_debug_logging(cli, capsys):
    cli(InvalidInputError("x"), "-v")
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
