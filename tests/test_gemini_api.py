import base64
import json

import pytest

from poop_detector.config import Config
from poop_detector.video.judge_failures import JudgeCallError
from poop_detector.video.judge_models import list_models
from poop_detector.video.providers import build_judge
from poop_detector.video.review import TRAY_SCHEMA
from poop_detector.video.token_usage import TokenUsage


class Response:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def read(self, limit):
        return self.body[:limit]


@pytest.fixture
def gemini(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=test-key\n")
    requests = []
    responses = []

    class Connection:
        def __init__(self, host, *, timeout):
            assert host == "generativelanguage.googleapis.com"
            assert timeout > 0

        def request(self, method, path, body, headers):
            requests.append((method, path, body, headers))

        def getresponse(self):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        def close(self):
            pass

    from poop_detector.video import gemini_api

    monkeypatch.setattr(gemini_api, "HTTPSConnection", Connection)
    return requests, responses


def test_config_accepts_api_as_primary_and_fallback(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("review: {provider: gemini-api, fallback: 'claude'}\n")
    config = Config.load(path)
    assert config.review_provider == "gemini-api"
    assert config.fallback_providers == ("claude",)
    path.write_text("review: {provider: codex, fallback: 'claude, gemini-api'}\n")
    assert Config.load(path).fallback_providers == ("claude", "gemini-api")


def test_lists_gemini_text_models_from_all_pages(gemini):
    requests, responses = gemini

    def model(name, method="generateContent"):
        return {"name": f"models/{name}", "supportedGenerationMethods": [method]}

    pages = (
        {
            "models": [
                model("gemini-4-argon"),
                model("gemini-3.1-flash-image"),
                model("gemini-3.8-flash-tts"),
                model("gemini-embedding-001", "embedContent"),
                model("gemma-4"),
                {
                    "name": "gemini-no-resource-prefix",
                    "supportedGenerationMethods": ["generateContent"],
                },
                model("gemini-invalid/id"),
            ],
            "nextPageToken": "next+page/2=",
        },
        {"models": [model("gemini-4-neon"), model("gemini-4-argon")]},
    )
    responses.extend(Response(200, json.dumps(page).encode()) for page in pages)

    assert list_models("gemini-api") == ("gemini-4-argon", "gemini-4-neon")
    assert [(method, path, body) for method, path, body, _ in requests] == [
        ("GET", "/v1beta/models?pageSize=100", None),
        ("GET", "/v1beta/models?pageSize=100&pageToken=next%2Bpage%2F2%3D", None),
    ]
    assert all(headers == {"x-goog-api-key": "test-key"} for _, _, _, headers in requests)


def test_gemini_catalog_rejects_repeated_cursors(gemini):
    requests, responses = gemini
    responses.extend([Response(200, b'{"models":[],"nextPageToken":"repeat"}')] * 2)
    with pytest.raises(ValueError, match="モデル一覧"):
        list_models("gemini-api")
    assert len(requests) == 2


@pytest.mark.parametrize("response", [Response(403, b"private response"), Response(200, b"{}")])
def test_gemini_catalog_reports_error_or_empty_list(gemini, response):
    _, responses = gemini
    responses.append(response)
    with pytest.raises(ValueError, match="モデル一覧") as error:
        list_models("gemini-api")
    assert "private response" not in str(error.value)


def test_check_fetches_model_without_generating(gemini):
    requests, responses = gemini
    responses.append(Response(200, b'{"supportedGenerationMethods":["generateContent"]}'))
    build_judge("gemini-api").check()
    method, path, body, headers = requests[0]
    assert (method, path, body) == ("GET", "/v1beta/models/gemini-3.8-flash", None)
    assert headers["x-goog-api-key"] == "test-key"


def test_sends_images_and_schema_and_measures_tokens(gemini, tmp_path, monkeypatch):
    requests, responses = gemini
    monkeypatch.setenv("GEMINI_API_KEY", "environment-key")
    (tmp_path / "frame.jpg").write_bytes(b"image")
    result = dict.fromkeys(TRAY_SCHEMA["required"])
    result["label"] = "none"
    responses.append(
        Response(
            200,
            json.dumps(
                {
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "parts": [
                                    {"thought": True, "text": "推論"},
                                    {"text": json.dumps(result)},
                                ]
                            },
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 100,
                        "cachedContentTokenCount": 20,
                        "candidatesTokenCount": 10,
                        "thoughtsTokenCount": 5,
                    },
                }
            ).encode(),
        )
    )
    judge = build_judge("gemini-api", "gemini-3.5-flash-lite")
    assert judge.judge(tmp_path, "判定", ["frame.jpg"], TRAY_SCHEMA) == result
    assert judge.last_usage == TokenUsage(100, 20, 15)
    method, path, body, headers = requests[0]
    assert (method, path) == ("POST", "/v1beta/models/gemini-3.5-flash-lite:generateContent")
    assert headers["x-goog-api-key"] == "environment-key"
    payload = json.loads(body)
    assert payload["contents"][0]["parts"] == [
        {"text": "判定"},
        {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(b"image").decode()}},
    ]
    assert payload["generationConfig"] == {
        "responseMimeType": "application/json",
        "responseJsonSchema": TRAY_SCHEMA,
    }


@pytest.mark.parametrize(
    ("response", "kind", "detail"),
    [
        (Response(401, b"test-key"), "login", "http:401"),
        (Response(402, b"test-key"), "rejected", "http:402"),
        (Response(403, b"test-key"), "login", "http:403"),
        (Response(429, b"test-key"), "rate_limited", "http:429"),
        (Response(503, b"test-key"), "connection", "http:503"),
        (Response(302, b"test-key"), "rejected", "http:302"),
        (TimeoutError("test-key"), "timeout", None),
        (OSError("test-key"), "connection", None),
        (Response(200, b"test-key"), "invalid_output", None),
        (Response(200, b" " * (1024 * 1024 + 1)), "invalid_output", None),
        (Response(200, b'{"candidates":[]}'), "invalid_output", None),
        (Response(200, b'{"candidates":[{"finishReason":"SAFETY"}]}'), "rejected", None),
        (
            Response(
                200, b'{"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"{}"}]}}]}'
            ),
            "invalid_output",
            None,
        ),
    ],
)
def test_errors_are_classified_without_exposing_response(gemini, tmp_path, response, kind, detail):
    _, responses = gemini
    responses.append(response)
    judge = build_judge("gemini-api")
    with pytest.raises(JudgeCallError) as caught:
        judge.judge(tmp_path, "判定", [], TRAY_SCHEMA)
    assert (caught.value.kind, caught.value.detail) == (kind, detail)
    assert "test-key" not in str(caught.value)
    assert judge.last_usage is None


def test_missing_key_and_invalid_model_are_rejected_before_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(JudgeCallError, match="GEMINI_API_KEY"):
        build_judge("gemini-api").check()
    with pytest.raises(ValueError, match="モデル名"):
        build_judge("gemini-api", "other/path")
    with pytest.raises(ValueError, match="推論の深さ"):
        build_judge("gemini-api", effort="high")


def test_check_rejects_unsupported_models(gemini):
    _, responses = gemini
    responses.append(Response(200, b'{"supportedGenerationMethods":["embedContent"]}'))
    with pytest.raises(JudgeCallError, match="generateContent"):
        build_judge("gemini-api", "embedding-model").check()
