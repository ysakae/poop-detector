"""判定ログに残った Gemini API のトークン数と参考費用をモデル別に集計する。"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .judge_runs import REVIEW_PURPOSES, JudgeRunUsage
from .token_usage import TokenUsage

# Google AI Studio の有料 Standard 単価（100万トークンあたりの米ドル）。
# https://aistudio.google.com/docs/pricing
_STANDARD_RATES = {
    "gemini-3.5-flash-lite": (Decimal("0.30"), Decimal("0.03"), Decimal("2.50")),
    "gemini-3.8-flash": (Decimal("0.75"), Decimal("0.075"), Decimal("3.75")),
    "gemini-4-argon": (Decimal("2.00"), Decimal("0.10"), Decimal("10.00")),
    "gemini-4-carbon": (Decimal("2.00"), Decimal("0.10"), Decimal("10.00")),
    "gemini-4-hydrogen": (Decimal("1.50"), Decimal("0.075"), Decimal("7.50")),
    "gemini-4-neon": (Decimal("3.00"), Decimal("0.15"), Decimal("15.00")),
}
_RATE_DOUBLES_ON = {
    "gemini-3.8-flash": date(2027, 1, 1),
    "gemini-4-argon": date(2026, 12, 1),
    "gemini-4-carbon": date(2026, 12, 1),
}


@dataclass
class GeminiModelUsage:
    """直近30日間にこのアプリが呼んだ、1モデル分の使用量。"""

    model: str | None
    calls: int = 0
    measured: int = 0
    tokens: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0, 0))

    def estimated_usd(self, as_of: date) -> Decimal | None:
        """表示時点の参考単価で、トークン数を取得できた呼び出しだけを計算する。"""
        model = self.model or ""
        rates = _STANDARD_RATES.get(model)
        if not self.measured or rates is None:
            return None
        input_rate, cached_rate, output_rate = rates
        tokens = self.tokens
        cost = (
            (tokens.input_tokens - tokens.cached_tokens) * input_rate
            + tokens.cached_tokens * cached_rate
            + tokens.output_tokens * output_rate
        ) / 1_000_000
        change = _RATE_DOUBLES_ON.get(model)
        if change is not None and as_of >= change:
            return cost * 2
        return cost


def summarize_gemini_usage(runs: Iterable[JudgeRunUsage]) -> list[GeminiModelUsage]:
    """動画の判定と判定器の比較をまとめ、Gemini API のモデルごとの合計を返す。"""
    by_model: dict[str | None, GeminiModelUsage] = {}
    for run in runs:
        if (
            run.provider != "gemini-api"
            or run.purpose not in REVIEW_PURPOSES
            or not run.last_30_days
        ):
            continue
        model_name = run.model or None
        model = by_model.setdefault(model_name, GeminiModelUsage(model_name))
        model.calls += run.last_30_days
        model.measured += run.measured
        model.tokens += TokenUsage(run.input_tokens, run.cached_tokens, run.output_tokens)
    return list(by_model.values())
