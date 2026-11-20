"""判定器の呼び出しの失敗を、CLI の出力を残さずに決まった種類へ分ける。

判定器が止まったと知らせるだけでは、タイムアウトか利用制限かが分からない。台帳とログには
ここで決めた種類と、番号や決まった語だけの詳細を残し、CLI の文言（アカウントや応答の本文を
含みうる）は残さない。
"""

import json
import re
import subprocess
from dataclasses import dataclass

# 失敗の種類と、画面に出す名前。
FAILURE_NAMES = {
    "timeout": "タイムアウト",
    "rate_limited": "利用制限・混雑",
    "login": "未ログイン",
    "launch": "起動できない",
    "exit": "異常終了",
    "connection": "通信の失敗",
    "rejected": "要求の拒否",
    "invalid_output": "判定の形式の誤り",
    "tool_request": "ツールの要求",
    "unsupported": "未対応の版",
    "other": "その他",
}
FAILURE_KINDS = tuple(FAILURE_NAMES)
# 詳細に入れてよいのは、終了コード・エラーの番号・停止の理由・例外の型名などの短い語だけ。
_DETAIL = re.compile(r"[A-Za-z0-9_:\-]{1,40}")
# 利用枠の超過や混雑を示す言い回し。種類を決めるためだけに照らし、文言は残さない。
_RATE_LIMIT = re.compile(
    r"quota|rate.?limit|resource.?exhausted|too many requests|\b429\b|overloaded"
    r"|capacity|usage limit",
    re.IGNORECASE,
)


def _checked(kind: str, detail: str | None) -> tuple[str, str | None]:
    if kind not in FAILURE_NAMES:
        raise ValueError(f"判定器の失敗の種類は {'、'.join(FAILURE_KINDS)} のいずれかです")
    if detail is not None and not _DETAIL.fullmatch(detail):
        raise ValueError("判定器の失敗の詳細には、番号や決まった語だけを入れてください")
    return kind, detail


class JudgeCallError(ValueError):
    """判定器の呼び出しの失敗。利用者向けの文言に加え、記録する種類と詳細を持つ。"""

    def __init__(self, message: str, kind: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.kind, self.detail = _checked(kind, detail)


@dataclass(frozen=True)
class FailureCause:
    """記録する失敗の種類と、番号などの短い詳細（なければ None）。"""

    kind: str
    detail: str | None = None


def failure_cause(error: BaseException) -> FailureCause:
    """例外から失敗の種類を決める。判定器が種類を付けていない例外は、型から推し量る。"""
    if isinstance(error, JudgeCallError):
        return FailureCause(error.kind, error.detail)
    if isinstance(error, subprocess.TimeoutExpired | TimeoutError):
        return FailureCause("timeout")
    if isinstance(error, subprocess.CalledProcessError):
        return FailureCause("exit", f"exit:{int(error.returncode)}")
    if isinstance(error, json.JSONDecodeError):
        return FailureCause("invalid_output")
    if isinstance(error, OSError):
        return FailureCause("launch")
    return FailureCause("other", type(error).__name__[:40])


def mentions_rate_limit(text: object) -> bool:
    """CLI の文言やエラーの中身が、利用枠の超過や混雑を示しているか。"""
    if text is None:
        return False
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    return bool(_RATE_LIMIT.search(text))


def rate_limited(failure: JudgeCallError, *evidence: object) -> JudgeCallError:
    """どれかの文言が利用制限を示していれば、元の種類を詳細に移して利用制限とする。"""
    if failure.kind == "rate_limited" or not any(map(mentions_rate_limit, evidence)):
        return failure
    return JudgeCallError(str(failure), "rate_limited", failure.kind)
