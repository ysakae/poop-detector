"""BigQuery の請求エクスポートから、開発者クレジットの消費と残高を集計する。"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..config import load_secret_settings
from .providers import _cli_environment

KEYS = (
    "POOP_GEMINI_BILLING_TABLE",
    "POOP_GEMINI_CREDIT_ID",
    "POOP_GEMINI_CREDIT_AMOUNT",
    "POOP_GEMINI_CREDIT_CURRENCY",
    "POOP_GEMINI_CREDIT_START_DATE",
)
TABLE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\.[A-Za-z0-9_]{1,1024}\.[A-Za-z0-9_]{1,1024}")
CREDIT_ID = re.compile(r"[^\x00-\x1f\x7f]{1,200}")
MAX_BYTES_BILLED = 1024**3
TIMEOUT = 45
RESPONSE_LIMIT = 64 * 1024
UNCONFIGURED = "Gemini API のクレジット集計を .env に設定してください。"
INVALID_SETTINGS = "Gemini API のクレジット集計の設定値を確認してください。"
NOT_INSTALLED = "BigQuery CLI（bq）をインストールし、PATH に追加してください。"
UNAVAILABLE = "BigQuery に問い合わせできません。認証、閲覧権限、対象テーブルを確認してください。"
NO_EXPORT = "指定した開始日と通貨の請求データがまだありません。"
NO_CREDIT = (
    "指定したクレジット ID の適用実績がまだありません。ID と請求データの反映を確認してください。"
)

QUERY = """SELECT
    COALESCE(-SUM(CAST(credit.amount AS NUMERIC)), 0) AS used,
    COALESCE(-SUM(IF(billing.service.description = 'Gemini API',
                     CAST(credit.amount AS NUMERIC), 0)), 0) AS gemini_used,
    COUNT(credit.id) AS credit_rows,
    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', MAX(billing.export_time), 'UTC') AS exported_at
FROM `{table}` AS billing
LEFT JOIN UNNEST(billing.credits) AS credit
    ON credit.id = @credit_id AND credit.type = 'PROMOTION'
WHERE billing.usage_start_time >= TIMESTAMP(@start_date) AND billing.currency = @currency
"""


@dataclass(frozen=True)
class CreditUsage:
    """1件の開発者クレジットについて、付与額と請求エクスポートに反映された消費額。"""

    checked_at: datetime
    exported_at: datetime | None = None
    currency: str = ""
    granted: Decimal = Decimal(0)
    used: Decimal | None = None
    gemini_used: Decimal | None = None
    problem: str | None = None

    @property
    def remaining(self) -> Decimal | None:
        if self.used is None:
            return None
        return max(Decimal(0), self.granted - self.used)


def _amount(value: object) -> Decimal:
    if not isinstance(value, str | int) or isinstance(value, bool) or len(str(value)) > 64:
        raise ValueError
    amount = Decimal(value)
    if not amount.is_finite() or not 0 <= amount < Decimal("1e20"):
        raise ValueError
    return amount


def _read_export(command: list[str], usage: CreditUsage) -> CreditUsage:
    env = _cli_environment()
    # bq が探す gcloud の設定場所を引き継ぐ。
    if "CLOUDSDK_CONFIG" in os.environ:
        env["CLOUDSDK_CONFIG"] = os.environ["CLOUDSDK_CONFIG"]
    try:
        # 解決した bq に固定のクエリと検証済みの値だけを渡す。
        response = subprocess.run(  # noqa: S603
            command,
            env=env,
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=False,
        )
        if response.returncode or len(response.stdout) > RESPONSE_LIMIT:
            return replace(usage, problem=UNAVAILABLE)
        (row,) = json.loads(response.stdout)
        if row["exported_at"] is None:
            return replace(usage, problem=NO_EXPORT)
        exported_at = datetime.fromisoformat(row["exported_at"])
        if exported_at.tzinfo is None:
            raise ValueError
        if int(row["credit_rows"]) == 0:
            return replace(usage, exported_at=exported_at, problem=NO_CREDIT)
        return replace(
            usage,
            exported_at=exported_at,
            used=_amount(row["used"]),
            gemini_used=_amount(row["gemini_used"]),
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, InvalidOperation):
        return replace(usage, problem=UNAVAILABLE)


def read_credits(now: datetime | None = None) -> CreditUsage:
    """サーバーに設定した付与分を集計する。表示に使える結果と案内だけを返す。"""
    checked_at = now or datetime.now(UTC)
    settings = load_secret_settings(KEYS)
    table, credit_id, amount, currency, start = (settings[key].strip() for key in KEYS)
    if not any((table, credit_id, amount, currency, start)):
        return CreditUsage(checked_at, problem=UNCONFIGURED)
    try:
        granted = _amount(amount)
        start = date.fromisoformat(start).isoformat()
        if (
            not TABLE.fullmatch(table)
            or not CREDIT_ID.fullmatch(credit_id)
            or not re.fullmatch(r"[A-Z]{3}", currency)
            or granted == 0
        ):
            raise ValueError
    except (ValueError, InvalidOperation):
        return CreditUsage(checked_at, problem=INVALID_SETTINGS)

    executable = shutil.which("bq")
    if executable is None:
        return CreditUsage(checked_at, problem=NOT_INSTALLED)
    usage = CreditUsage(checked_at, currency=currency, granted=granted)
    return _read_export(
        [
            str(Path(executable).resolve()),
            f"--bigqueryrc={os.devnull}",
            f"--project_id={table.split('.')[0]}",
            "--format=json",
            "query",
            "--use_legacy_sql=false",
            f"--maximum_bytes_billed={MAX_BYTES_BILLED}",
            f"--parameter=credit_id:STRING:{credit_id}",
            f"--parameter=start_date:DATE:{start}",
            f"--parameter=currency:STRING:{currency}",
            QUERY.format(table=table),
        ],
        usage,
    )


class GeminiCreditBoard:
    """管理者が問い合わせた直近の結果を保持し、同時の問い合わせを1件にまとめる。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.latest: CreditUsage | None = None

    def refresh(self) -> None:
        if not self.refresh_lock.acquire(blocking=False):
            return
        try:
            usage = read_credits()
            with self.lock:
                self.latest = usage
        finally:
            self.refresh_lock.release()

    def snapshot(self) -> CreditUsage | None:
        with self.lock:
            return self.latest
