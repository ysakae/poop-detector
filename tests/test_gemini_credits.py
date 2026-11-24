import json
import subprocess
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from poop_detector.video import gemini_credits

SETTINGS = {
    "POOP_GEMINI_BILLING_TABLE": "billing-project.cost.gcp_billing_export_v1_111111_222222_333333",
    "POOP_GEMINI_CREDIT_ID": "google-ai-pro-credit",
    "POOP_GEMINI_CREDIT_AMOUNT": "1500",
    "POOP_GEMINI_CREDIT_CURRENCY": "JPY",
    "POOP_GEMINI_CREDIT_START_DATE": "2026-11-24",
}
ROW = {
    "used": "375.50",
    "gemini_used": "210.20",
    "credit_rows": "2",
    "exported_at": "2026-11-24T06:00:00Z",
}
NOW = datetime(2026, 11, 25, 3, tzinfo=UTC)


@pytest.fixture
def billing(monkeypatch):
    monkeypatch.setattr(gemini_credits, "load_secret_settings", lambda keys: SETTINGS.copy())
    monkeypatch.setattr(gemini_credits.shutil, "which", lambda name: "/fake/bq")


def test_credit_query_reads_the_export_and_computes_the_balance(billing, monkeypatch):
    def run(command, **kwargs):
        query = command[-1]
        assert command[0] == "/fake/bq"
        assert "--project_id=billing-project" in command
        assert "--parameter=credit_id:STRING:google-ai-pro-credit" in command
        assert "--parameter=start_date:DATE:2026-11-24" in command
        assert "--parameter=currency:STRING:JPY" in command
        assert "--maximum_bytes_billed=1073741824" in command
        assert "credit.id = @credit_id" in query
        assert "usage_start_time >= TIMESTAMP(@start_date)" in query
        assert "Gemini API" in query
        assert "GEMINI_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 0, json.dumps([ROW]).encode())

    monkeypatch.setenv("GEMINI_API_KEY", "private-key")
    monkeypatch.setattr(gemini_credits.subprocess, "run", run)
    result = gemini_credits.read_credits(NOW)
    assert result.problem is None
    assert result.currency == "JPY"
    assert result.granted == Decimal("1500")
    assert result.used == Decimal("375.50")
    assert result.gemini_used == Decimal("210.20")
    assert result.remaining == Decimal("1124.50")
    assert result.exported_at == datetime(2026, 11, 24, 6, tzinfo=UTC)
    assert result.checked_at == NOW


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        ({**ROW, "exported_at": None}, gemini_credits.NO_EXPORT),
        ({**ROW, "credit_rows": "0"}, gemini_credits.NO_CREDIT),
        ({**ROW, "used": "NaN"}, gemini_credits.UNAVAILABLE),
    ],
)
def test_missing_or_invalid_export_does_not_report_a_balance(billing, monkeypatch, row, problem):
    monkeypatch.setattr(
        gemini_credits.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps([row]).encode()
        ),
    )
    result = gemini_credits.read_credits(NOW)
    assert result.problem == problem
    assert result.remaining is None


def test_invalid_settings_are_rejected_before_querying(monkeypatch):
    settings = {**SETTINGS, "POOP_GEMINI_BILLING_TABLE": "billing-project.cost.test`;"}
    monkeypatch.setattr(gemini_credits, "load_secret_settings", lambda keys: settings)
    monkeypatch.setattr(
        gemini_credits.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("問い合わせが実行された"),
    )
    result = gemini_credits.read_credits(NOW)
    assert result.problem == gemini_credits.INVALID_SETTINGS
    assert result.remaining is None


def test_permission_errors_hide_cli_output(billing, monkeypatch):
    monkeypatch.setattr(
        gemini_credits.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, b"private details"),
    )
    assert gemini_credits.read_credits(NOW).problem == gemini_credits.UNAVAILABLE
