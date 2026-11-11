"""選択した外部判定器へ、切り出し済みの画像と判定条件を渡す。"""

import base64
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.request import Request
from urllib.request import urlopen as open_request

from ..config import load_secret_settings
from .codex_cli import CodexCliJudge

if TYPE_CHECKING:
    from .review import VideoJudge

KEY_NAMES = {"claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
RESPONSE_LIMIT = 1024 * 1024


class ApiJudge:
    """Claude API または Gemini API から、共通スキーマの判定を取得する。"""

    def __init__(self, provider: str, model: str, api_key: str) -> None:
        self.provider, self.model = provider, model
        self.name = {"claude": "Claude API", "gemini": "Gemini API"}[provider]
        self._api_key = api_key

    def check(self) -> None:
        if not self.model:
            raise ValueError(f"{self.name} を使うには review.model を設定してください")
        if not self._api_key:
            raise ValueError(
                f"{self.name} を使うには {KEY_NAMES[self.provider]} を設定してください"
            )

    def judge(
        self, folder: Path, prompt: str, images: Sequence[str], schema: dict[str, Any]
    ) -> object:
        self.check()
        encoded = [
            base64.b64encode((folder / image).read_bytes()).decode("ascii") for image in images
        ]
        if self.provider == "claude":
            endpoint = "https://api.anthropic.com/v1/messages"
            key_header = "x-api-key"
            payload = {
                "model": self.model,
                "max_tokens": 2048,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            *(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/jpeg",
                                        "data": image,
                                    },
                                }
                                for image in encoded
                            ),
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                "output_config": {"format": {"type": "json_schema", "schema": schema}},
            }
        else:
            endpoint = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent"
            )
            key_header = "x-goog-api-key"
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            *(
                                {"inlineData": {"mimeType": "image/jpeg", "data": image}}
                                for image in encoded
                            ),
                            {"text": prompt},
                        ],
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                },
            }
        # 送信先は上で選んだ固定の HTTPS ホストだけ。
        request = Request(  # noqa: S310
            endpoint,
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
            method="POST",
        )
        # リダイレクト先に認証ヘッダーを転送しない。
        request.add_unredirected_header(key_header.capitalize(), self._api_key)
        if self.provider == "claude":
            request.add_header("anthropic-version", "2023-06-01")
        try:
            # 固定の HTTPS 送信先に対してだけ要求を送る。
            with open_request(request, timeout=120) as response:  # noqa: S310
                content = response.read(RESPONSE_LIMIT + 1)
            if len(content) > RESPONSE_LIMIT:
                raise ValueError
            return self._decode(json.loads(content))
        except OSError:
            raise ValueError(
                f"{self.name} に接続できません。認証・接続・利用上限を確認してください"
            ) from None
        except (IndexError, KeyError, TypeError, ValueError, AttributeError):
            raise ValueError(f"{self.name} から有効な判定を取得できません") from None

    def _decode(self, response: dict[str, Any]) -> object:
        if self.provider == "claude":
            if response.get("stop_reason", "end_turn") != "end_turn":
                raise ValueError
            parts = [part for part in response["content"] if part.get("type") == "text"]
        else:
            candidate = response["candidates"][0]
            if candidate.get("finishReason", "STOP") != "STOP":
                raise ValueError
            parts = [part for part in candidate["content"]["parts"] if not part.get("thought")]
        return json.loads("".join(part["text"] for part in parts))


def build_judge(provider: str, model: str = "") -> "VideoJudge":
    if provider == "codex":
        return CodexCliJudge(model=model)
    if provider not in KEY_NAMES:
        raise ValueError("判定器には codex、claude、gemini のいずれかを指定してください")
    key_name = KEY_NAMES[provider]
    return ApiJudge(provider, model, load_secret_settings((key_name,))[key_name].strip())
