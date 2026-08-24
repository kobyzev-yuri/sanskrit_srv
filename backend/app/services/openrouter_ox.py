"""OpenRouter stealth/ox-alpha call shape.

ox-alpha is free and reasoning-mandatory. Catalog default effort is ``max``, which
burns 5–15 minutes per page and often fills the 32k completion cap with chain-of-thought
instead of HTML. We keep the model (quality + price) and turn the knobs:

- translate: effort=low (HTML transform of known source)
- digitize: effort=high (vision fidelity, still not max)
- completion cap well below 32k so thinking cannot starve the HTML
- retry 429 from the shared free pool instead of skipping the page
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from app.config import get_settings
from app.services.llm_status import LlmQuotaError, LlmRateLimitError, is_quota_response, set_quota_alert

log = logging.getLogger("sanskrit.openrouter")

TASK_TRANSLATE = "translate"
TASK_DRAFT = "draft"

# Catalog supports max | high | low (no medium). Default if omitted: max.
_EFFORT = {
    TASK_TRANSLATE: "low",
    TASK_DRAFT: "high",
}
_COMPLETION_CAP = {
    TASK_TRANSLATE: 8192,
    TASK_DRAFT: 16384,
}

_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_BACKOFF_S = (20, 45, 90, 120)


def is_ox_model(model: str) -> bool:
    mid = (model or "").lower()
    return "ox-alpha" in mid or mid.startswith("stealth/")


def completion_cap(task: str) -> int:
    """Task cap; OPENROUTER_MAX_TOKENS may only lower it (never restore 32k marathons)."""
    default = _COMPLETION_CAP.get(task, 8192)
    env = int(get_settings().openrouter_max_tokens or 0)
    if env >= 1024:
        return min(env, default)
    return default


def reasoning_effort(task: str) -> str:
    return _EFFORT.get(task, "low")


def apply_ox_chat_options(
    payload: dict[str, Any],
    model: str,
    *,
    task: str,
) -> dict[str, Any]:
    """Attach ox-alpha reasoning/completion options (or generic max_tokens)."""
    limit = completion_cap(task)
    if is_ox_model(model):
        payload["max_completion_tokens"] = limit
        # exclude=False: some replies put the HTML only in `reasoning`; callers parse that.
        payload["reasoning"] = {
            "enabled": True,
            "effort": reasoning_effort(task),
            "exclude": False,
        }
    else:
        payload["temperature"] = 0
        payload["max_tokens"] = limit
    return payload


def openrouter_headers(api_key: str) -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": (settings.openrouter_http_referer or "https://sanskrit-srv.local"),
        "X-Title": (settings.openrouter_app_title or "sanskrit_srv"),
    }


def _retry_after_seconds(resp: httpx.Response, fallback: int) -> int:
    raw = (resp.headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return max(1, min(int(raw), 180))
    return fallback


def post_openrouter_chat(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float = 300,
    sleep: Callable[[float], None] = time.sleep,
    post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """POST /chat/completions. Retry upstream 429/5xx with backoff."""
    do_post = post or httpx.post
    waits = [0, *_BACKOFF_S]
    last_err = ""
    for attempt, wait in enumerate(waits):
        if wait:
            log.warning("OpenRouter retry in %ss (%s)", wait, last_err[:180])
            sleep(wait)
        resp = do_post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError("OpenRouter returned non-object JSON")
            return data
        body = (resp.text or "")[:400]
        if is_quota_response(resp.status_code, body):
            msg = "Лимит OpenRouter / оплата (HTTP 402)."
            set_quota_alert(msg)
            raise LlmQuotaError(msg)
        if resp.status_code in _RETRY_STATUSES:
            last_err = f"HTTP {resp.status_code} {body[:200]}"
            if attempt + 1 < len(waits):
                waits[attempt + 1] = max(
                    waits[attempt + 1],
                    _retry_after_seconds(resp, waits[attempt + 1]),
                )
            continue
        raise RuntimeError(f"HTTP {resp.status_code} {body[:300]}")
    raise LlmRateLimitError(last_err or "OpenRouter rate limited")
