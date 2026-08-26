"""Gemini AI Studio vs ProxyAPI request shape."""
import pytest

from app.services.gemini_client import generate_gemini_content, gemini_headers, is_google_studio_base
from app.services.llm_status import LlmQuotaError, LlmRateLimitError, is_quota_response


def test_studio_uses_goog_api_key_header():
    headers = gemini_headers("k", "https://generativelanguage.googleapis.com")
    assert headers["x-goog-api-key"] == "k"
    assert "Authorization" not in headers
    assert is_google_studio_base("https://generativelanguage.googleapis.com/v1beta")


def test_proxyapi_uses_bearer():
    headers = gemini_headers("px", "https://api.proxyapi.ru/google")
    assert headers["Authorization"] == "Bearer px"
    assert "x-goog-api-key" not in headers


def test_studio_parts_use_camel_case_inline_data():
    from app.services.gemini_client import _normalize_parts

    parts = _normalize_parts(
        [{"text": "hi"}, {"inline_data": {"mime_type": "image/jpeg", "data": "abc"}}],
        studio=True,
    )
    assert parts[0] == {"text": "hi"}
    assert parts[1]["inlineData"]["mimeType"] == "image/jpeg"
    assert parts[1]["inlineData"]["data"] == "abc"


class _Resp:
    def __init__(self, status, text="", json_data=None, headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self._json = json_data or {}

    def json(self):
        return self._json


def test_generate_retries_429_then_succeeds():
    calls = {"n": 0}
    ok = {"candidates": [{"content": {"parts": [{"text": "OK"}]}}], "usageMetadata": {}}

    def fake_post(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(429, '{"error":{"message":"RESOURCE_EXHAUSTED"}}', headers={"Retry-After": "1"})
        return _Resp(200, json_data=ok)

    slept: list[float] = []
    text, _usage = generate_gemini_content(
        model="gemini-3.1-pro-preview",
        parts=[{"text": "hi"}],
        api_key="k",
        base_url="https://generativelanguage.googleapis.com",
        sleep=slept.append,
        post=fake_post,
    )
    assert text == "OK"
    assert calls["n"] == 2
    assert slept == [12]


def test_generate_429_exhausted_raises_rate_limit():
    def always_429(*_a, **_k):
        return _Resp(429, "RESOURCE_EXHAUSTED")

    with pytest.raises(LlmRateLimitError, match="5 запросов в минуту"):
        generate_gemini_content(
            model="gemini-3.1-pro-preview",
            parts=[{"text": "hi"}],
            api_key="k",
            base_url="https://generativelanguage.googleapis.com",
            sleep=lambda _s: None,
            post=always_429,
        )


def test_quota_detects_studio_prepaid_credits():
    body = '{"error":{"code":429,"message":"Your prepayment credits are depleted. Please go to AI Studio"}}'
    assert is_quota_response(429, body)
    assert not is_quota_response(429, "RESOURCE_EXHAUSTED rate-limited upstream")


def test_generate_credits_depleted_is_quota_not_retry():
    calls = {"n": 0}

    def once(*_a, **_k):
        calls["n"] += 1
        return _Resp(429, "Your prepayment credits are depleted. Please go to AI Studio")

    with pytest.raises(LlmQuotaError, match="кредиты"):
        generate_gemini_content(
            model="gemini-3.1-pro-preview",
            parts=[{"text": "hi"}],
            api_key="k",
            base_url="https://generativelanguage.googleapis.com",
            sleep=lambda _s: None,
            post=once,
        )
    assert calls["n"] == 1


def test_daily_quota_429_does_not_retry():
    from app.services.llm_status import gemini_quota_wait_message

    body = (
        '{"error":{"code":429,"message":"You exceeded your current quota",'
        '"details":[{"retryDelay":"7200s"},'
        '{"quotaId":"GenerateRequestsPerDayPerProjectPerModel"}]}}'
    )
    msg = gemini_quota_wait_message(body, 7200)
    assert "Суточный" in msg
    assert "ч" in msg

    calls = {"n": 0}

    def once(*_a, **_k):
        calls["n"] += 1
        return _Resp(429, body, json_data=__import__("json").loads(body))

    with pytest.raises(LlmRateLimitError, match="Суточный"):
        generate_gemini_content(
            model="gemini-3.5-flash",
            parts=[{"text": "hi"}],
            api_key="k",
            base_url="https://generativelanguage.googleapis.com",
            sleep=lambda _s: None,
            post=once,
        )
    assert calls["n"] == 1


def test_format_wait_ru():
    from app.services.llm_status import format_wait_ru

    assert format_wait_ru(20) == "20 сек"
    assert "мин" in format_wait_ru(120)
    assert format_wait_ru(7200) == "2 ч"
