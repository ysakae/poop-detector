"""判定器の選択と画像付き構造化判定を、外部通信せずに確認する。"""

import base64
import io
import json

import pytest

from poop_detector.commands.context import CommandError
from poop_detector.commands.review import ready_judge
from poop_detector.config import Config


@pytest.mark.parametrize(
    ("provider", "key", "header", "response"),
    [
        (
            "claude",
            "ANTHROPIC_API_KEY",
            "X-api-key",
            {"content": [{"type": "text", "text": '{"label":"urine"}'}]},
        ),
        (
            "gemini",
            "GEMINI_API_KEY",
            "X-goog-api-key",
            {"candidates": [{"content": {"parts": [{"text": '{"label":"urine"}'}]}}]},
        ),
    ],
)
def test_selected_api_uses_the_requested_model_and_image(
    tmp_path, monkeypatch, provider, key, header, response
):
    monkeypatch.setenv(key, "fake-key")
    calls = []

    def open_request(request, *, timeout):
        calls.append(request)
        return io.BytesIO(json.dumps(response).encode())

    judge = ready_judge(Config(review_provider=provider, review_model="test-model"))
    monkeypatch.setattr("poop_detector.video.providers.open_request", open_request)
    (tmp_path / "frames.jpg").write_bytes(b"image-for-test")
    schema = {"type": "object"}
    assert judge.judge(tmp_path, "判定してください", ["frames.jpg"], schema) == {"label": "urine"}
    assert len(calls) == 1
    assert calls[0].get_header(header) == "fake-key"
    request_body = calls[0].data.decode()
    assert base64.b64encode(b"image-for-test").decode() in request_body
    assert "fake-key" not in request_body
    if provider == "claude":
        assert json.loads(request_body)["model"] == "test-model"
        assert json.loads(request_body)["output_config"]["format"]["schema"] == schema
    else:
        assert calls[0].full_url.endswith("/models/test-model:generateContent")
        assert json.loads(request_body)["generationConfig"]["responseJsonSchema"] == schema


def test_selected_provider_reports_missing_credentials_without_falling_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(CommandError, match="ANTHROPIC_API_KEY"):
        ready_judge(Config(review_provider="claude", review_model="test-model"))


def test_api_errors_do_not_expose_key_or_provider_response(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    judge = ready_judge(Config(review_provider="gemini", review_model="test-model"))

    def fail(request, *, timeout):
        raise TimeoutError("fake-key")

    monkeypatch.setattr("poop_detector.video.providers.open_request", fail)
    with pytest.raises(ValueError, match="接続できません") as error:
        judge.judge(tmp_path, "判定", [], {"type": "object"})
    assert "fake-key" not in str(error.value)

    monkeypatch.setattr(
        "poop_detector.video.providers.open_request",
        lambda request, *, timeout: io.BytesIO(b'{"error":"fake-key"}'),
    )
    with pytest.raises(ValueError, match="有効な判定") as error:
        judge.judge(tmp_path, "判定", [], {"type": "object"})
    assert "fake-key" not in str(error.value)
