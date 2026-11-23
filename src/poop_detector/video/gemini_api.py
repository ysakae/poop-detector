"""Gemini API に切り出し画像を送り、構造化された判定を受け取る。"""

import base64
import json
import re
from collections.abc import Sequence
from contextlib import closing
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from typing import Any

from ..config import load_secret_settings
from .codex_cli import JUDGE_TIMEOUT
from .judge_failures import JudgeCallError
from .token_usage import OUTPUT_LIMIT, TokenUsage, parse_gemini

API_HOST = "generativelanguage.googleapis.com"
API_KEY = "GEMINI_API_KEY"
DEFAULT_MODEL = "gemini-3.8-flash"
GEMINI_API_MODELS = (DEFAULT_MODEL, "gemini-3.5-flash-lite")
CHECK_TIMEOUT = 15
INVALID_OUTPUT = "Gemini API から有効な判定を取得できません"
HTTP_ERRORS = {
    400: ("要求が拒否されました。API キーとモデルの設定を確認してください", "rejected"),
    401: ("API キーを確認してください", "login"),
    402: ("Google AI Studio の請求設定と前払い残高を確認してください", "rejected"),
    403: ("API キーとプロジェクトの権限を確認してください", "login"),
    404: ("モデルが見つかりません。Google AI Studio でモデル名を確認してください", "rejected"),
    429: ("利用上限に達したか、混雑しています", "rate_limited"),
}


def _request(method: str, path: str, timeout: int, body: bytes | None = None) -> dict[str, Any]:
    key = load_secret_settings((API_KEY,))[API_KEY].strip()
    if not key:
        raise JudgeCallError(f".env または環境変数に {API_KEY} を設定してください", "login")
    if not key.isascii() or not key.isprintable() or " " in key:
        raise JudgeCallError(f"{API_KEY} の形式を確認してください", "login")
    headers = {"x-goog-api-key": key}
    if body is not None:
        headers["Content-Type"] = "application/json"
    # 接続先を固定し、リダイレクトを追わないのでキーはこのホストにだけ送る。
    try:
        with closing(HTTPSConnection(API_HOST, timeout=timeout)) as connection:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            code = response.status
            if code != 200:
                message, kind = HTTP_ERRORS.get(
                    code, ("要求に失敗しました", "connection" if code >= 500 else "rejected")
                )
                raise JudgeCallError(f"Gemini API: {message}", kind, f"http:{code}")
            data = response.read(OUTPUT_LIMIT + 1)
    except TimeoutError:
        raise JudgeCallError("Gemini API の応答がタイムアウトしました", "timeout") from None
    except (OSError, HTTPException):
        raise JudgeCallError("Gemini API との通信に失敗しました", "connection") from None
    try:
        result = json.loads(data) if len(data) <= OUTPUT_LIMIT else None
    except (ValueError, RecursionError):
        result = None
    if not isinstance(result, dict):
        raise JudgeCallError(INVALID_OUTPUT, "invalid_output")
    return result


class GeminiApiJudge:
    """Google AI Studio の API キーで Gemini を呼び出す判定器。"""

    name = "Gemini API"

    def __init__(self, model: str) -> None:
        self.model = model or DEFAULT_MODEL
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.model):
            raise ValueError("Gemini API のモデル名は英数字で始まるモデル ID を指定してください")
        self.path = f"/v1beta/models/{self.model}"
        self.last_usage: TokenUsage | None = None

    def check(self) -> None:
        """生成を行わず、API キーで指定モデルの情報を取得できるか確かめる。"""
        model = _request("GET", self.path, CHECK_TIMEOUT)
        methods = model.get("supportedGenerationMethods")
        if not isinstance(methods, list) or "generateContent" not in methods:
            raise JudgeCallError(
                "Gemini API のモデルが generateContent に対応していません", "unsupported"
            )

    def judge(
        self, folder: Path, prompt: str, images: Sequence[str], schema: dict[str, Any]
    ) -> object:
        self.last_usage = None
        parts = [
            {"text": prompt},
            *(
                {
                    "inlineData": {
                        "mimeType": "image/jpeg",
                        "data": base64.b64encode((folder / image).read_bytes()).decode("ascii"),
                    }
                }
                for image in images
            ),
        ]
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        }
        response = _request(
            "POST", f"{self.path}:generateContent", JUDGE_TIMEOUT, json.dumps(body).encode()
        )
        self.last_usage = parse_gemini(response.get("usageMetadata"))
        return _assessment(response, schema)


def _assessment(response: dict[str, Any], schema: dict[str, Any]) -> object:
    try:
        candidate = response["candidates"][0]
        if candidate["finishReason"] != "STOP":
            raise JudgeCallError("Gemini API が判定を完了できませんでした", "rejected")
        text = "".join(
            part.get("text", "")
            for part in candidate["content"]["parts"]
            if not part.get("thought")
        )
        result = json.loads(text)
    except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError, RecursionError):
        raise JudgeCallError(INVALID_OUTPUT, "invalid_output") from None
    if not isinstance(result, dict) or result.keys() != set(schema["required"]):
        raise JudgeCallError(INVALID_OUTPUT, "invalid_output")
    return result
