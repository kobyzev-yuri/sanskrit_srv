"""ox-alpha OpenRouter payload + 429 retry."""
from types import SimpleNamespace

import pytest

from app.services.llm_status import LlmQuotaError, LlmRateLimitError
from app.services.openrouter_ox import (
    TASK_DRAFT,
    TASK_TRANSLATE,
    apply_ox_chat_options,
    completion_cap,
    post_openrouter_chat,
)


def test_ox_translate_payload_uses_low_effort_and_8k_cap():
    payload: dict = {"model": "stealth/ox-alpha"}
    apply_ox_chat_options(payload, "stealth/ox-alpha", task=TASK_TRANSLATE)
    assert payload["max_completion_tokens"] == 8192
    assert payload["reasoning"]["effort"] == "low"
    assert payload["reasoning"]["enabled"] is True
    assert "max_tokens" not in payload


def test_ox_draft_payload_uses_high_effort():
    payload: dict = {"model": "stealth/ox-alpha"}
    apply_ox_chat_options(payload, "stealth/ox-alpha", task=TASK_DRAFT)
    assert payload["max_completion_tokens"] == 16384
    assert payload["reasoning"]["effort"] == "high"


def test_env_32k_cannot_raise_translate_cap(monkeypatch):
    from app.services import openrouter_ox as ox

    monkeypatch.setattr(
        ox,
        "get_settings",
        lambda: SimpleNamespace(openrouter_max_tokens=32768),
    )
    assert completion_cap(TASK_TRANSLATE) == 8192
    assert completion_cap(TASK_DRAFT) == 16384


def test_env_can_lower_cap(monkeypatch):
    from app.services import openrouter_ox as ox

    monkeypatch.setattr(
        ox,
        "get_settings",
        lambda: SimpleNamespace(openrouter_max_tokens=4096),
    )
    assert completion_cap(TASK_TRANSLATE) == 4096


class _Resp:
    def __init__(self, status, text="", json_data=None, headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self._json = json_data or {}

    def json(self):
        return self._json


def test_post_retries_429_then_succeeds():
    calls = {"n": 0}
    ok = {"choices": [{"message": {"content": "<article></article>"}}]}

    def fake_post(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(429, "rate-limited upstream", headers={"Retry-After": "1"})
        return _Resp(200, json_data=ok)

    slept: list[float] = []
    data = post_openrouter_chat(
        "https://example/v1/chat/completions",
        headers={},
        payload={"model": "stealth/ox-alpha"},
        sleep=slept.append,
        post=fake_post,
    )
    assert data == ok
    assert calls["n"] == 2
    assert slept == [20]


def test_post_429_exhausted_raises_rate_limit():
    def always_429(*_a, **_k):
        return _Resp(429, "stealth/ox-alpha is temporarily rate-limited upstream")

    with pytest.raises(LlmRateLimitError):
        post_openrouter_chat(
            "https://example/v1/chat/completions",
            headers={},
            payload={},
            sleep=lambda _s: None,
            post=always_429,
        )


def test_post_402_is_quota():
    def paywall(*_a, **_k):
        return _Resp(402, "Payment required")

    with pytest.raises(LlmQuotaError):
        post_openrouter_chat(
            "https://example/v1/chat/completions",
            headers={},
            payload={},
            sleep=lambda _s: None,
            post=paywall,
        )
