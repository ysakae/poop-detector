"""判定器が1回の呼び出しで使ったトークン数を取り出す。

Codex CLI は `codex exec --json` の turn.completed、Claude Code CLI は結果の usage に数を出す。
Gemini API は usageMetadata に数を出す。Antigravity CLI は出さないので記録しない。型と範囲を確かめた数だけを持ち、応答の本文は持たない。
"""

import json
from dataclasses import dataclass

# CLI の出力を読む上限（バイト）と、1回の呼び出しとして受け入れるトークン数の上限。
OUTPUT_LIMIT = 1024 * 1024
TOKEN_LIMIT = 100_000_000


@dataclass(frozen=True)
class TokenUsage:
    """入力（キャッシュから読んだ分を含む）、そのうちキャッシュから読んだ分、出力のトークン数。"""

    input_tokens: int
    cached_tokens: int
    output_tokens: int

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.cached_tokens + other.cached_tokens,
            self.output_tokens + other.output_tokens,
        )


def _count(values: object, key: str, *, required: bool = False) -> int | None:
    """values[key] のトークン数。ないときは 0（required なら None）、数でなければ None。"""
    if not isinstance(values, dict):
        return None
    value = values.get(key)
    if value is None:
        return None if required else 0
    # bool は int の一種なので、先に除く。
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= TOKEN_LIMIT:
        return None
    return value


def _usage(input_tokens: int | None, cached: int | None, output: int | None) -> TokenUsage | None:
    if input_tokens is None or cached is None or output is None:
        return None
    return TokenUsage(input_tokens, cached, output)


def parse_codex(stdout: object) -> TokenUsage | None:
    """`codex exec --json` の JSONL から、turn.completed の使用量を足し合わせる。

    Codex の input_tokens はキャッシュから読んだ分を含み、output_tokens は推論の分を含む。
    """
    if not isinstance(stdout, bytes) or len(stdout) > OUTPUT_LIMIT:
        return None
    total: TokenUsage | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        values = event.get("usage")
        usage = _usage(
            _count(values, "input_tokens", required=True),
            _count(values, "cached_input_tokens"),
            _count(values, "output_tokens", required=True),
        )
        if usage is None:
            return None
        total = usage if total is None else total + usage
    return total


def parse_claude(values: object) -> TokenUsage | None:
    """Claude Code CLI の結果の usage。input_tokens はキャッシュの分を含まないので足す。"""
    fresh = _count(values, "input_tokens", required=True)
    read = _count(values, "cache_read_input_tokens")
    written = _count(values, "cache_creation_input_tokens")
    if fresh is None or read is None or written is None:
        return None
    return _usage(fresh + read + written, read, _count(values, "output_tokens", required=True))


def parse_gemini(values: object) -> TokenUsage | None:
    """Gemini API の使用量。入力にはキャッシュ分を含め、出力には推論分を足す。"""
    output = _count(values, "candidatesTokenCount", required=True)
    thoughts = _count(values, "thoughtsTokenCount")
    if output is None or thoughts is None:
        return None
    return _usage(
        _count(values, "promptTokenCount", required=True),
        _count(values, "cachedContentTokenCount"),
        output + thoughts,
    )
