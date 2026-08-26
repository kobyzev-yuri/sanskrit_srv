"""Gemini generateContent: Google AI Studio or ProxyAPI Google gateway."""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

import httpx

from app.config import get_settings
from app.services.llm_status import (
    GEMINI_CREDITS_MSG,
    GEMINI_RATE_LIMIT_MSG,
    LlmQuotaError,
    LlmRateLimitError,
    gemini_quota_wait_message,
    is_quota_response,
    set_quota_alert,
)
from app.services.llm_usage import parse_gemini_usage

log = logging.getLogger("sanskrit.gemini")

GOOGLE_GEMINI_BASE = "https://generativelanguage.googleapis.com"
PROXY_GEMINI_BASE = "https://api.proxyapi.ru/google"
GEMINI_MAX_OUTPUT_TOKENS = 32768
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_BACKOFF_S = (12, 25, 45)


def is_google_studio_base(base_url: str) -> bool:
    return "generativelanguage.googleapis.com" in (base_url or "").strip().lower()


def resolve_gemini_endpoint() -> tuple[str, str]:
    """Return (api_key, base_url). Studio key wins over ProxyAPI."""
    from app.services.llm_route import current_creds

    settings = get_settings()
    creds = current_creds()
    studio = (getattr(creds, "gemini_api_key", None) or "").strip()
    if studio:
        base = (settings.gemini_base_url or "").strip().rstrip("/")
        if not base or "proxyapi.ru" in base.lower():
            base = GOOGLE_GEMINI_BASE
        return studio, base
    proxy = (creds.openai_api_key or "").strip()
    base = (settings.gemini_base_url or PROXY_GEMINI_BASE).strip().rstrip("/")
    return proxy, base


def gemini_headers(api_key: str, base_url: str) -> dict[str, str]:
    if is_google_studio_base(base_url):
        return {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _normalize_parts(parts: list[dict[str, Any]], *, studio: bool) -> list[dict[str, Any]]:
    """Google REST wants camelCase inlineData; ProxyAPI accepted snake_case."""
    if not studio:
        return parts
    out: list[dict[str, Any]] = []
    for part in parts:
        blob = part.get("inline_data") or part.get("inlineData")
        if blob:
            out.append(
                {
                    "inlineData": {
                        "mimeType": blob.get("mime_type") or blob.get("mimeType") or "image/jpeg",
                        "data": blob.get("data") or "",
                    }
                }
            )
            continue
        out.append(part)
    return out


def _retry_after_seconds(resp: httpx.Response, fallback: int = 0, *, cap: int = 48 * 3600) -> int:
    raw = (resp.headers.get("Retry-After") or "").strip()
    parsed: float | None = None
    if raw.isdigit():
        parsed = float(raw)
    else:
        try:
            details = ((resp.json() or {}).get("error") or {}).get("details") or []
            for item in details:
                if not isinstance(item, dict):
                    continue
                delay = str(item.get("retryDelay") or "")
                match = re.match(r"([0-9.]+)\s*s", delay, re.I)
                if match:
                    parsed = float(match.group(1))
                    break
        except Exception:  # noqa: BLE001
            parsed = None
    if parsed is None:
        return max(0, int(fallback))
    return max(1, min(int(parsed), cap))


def generate_gemini_content(
    *,
    model: str,
    parts: list[dict[str, Any]],
    max_output_tokens: int = GEMINI_MAX_OUTPUT_TOKENS,
    api_key: str | None = None,
    base_url: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    post: Callable[..., Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    if not api_key or not base_url:
        resolved_key, resolved_base = resolve_gemini_endpoint()
        api_key = api_key or resolved_key
        base_url = base_url or resolved_base
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY missing (and no ProxyAPI key for Gemini)")
    url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent"
    studio = is_google_studio_base(base_url)
    payload = {
        "contents": [{"role": "user", "parts": _normalize_parts(parts, studio=studio)}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": int(max_output_tokens)},
    }
    do_post = post or httpx.post
    waits = [0, *_BACKOFF_S]
    last_err = ""
    resp: httpx.Response | None = None
    for attempt, wait in enumerate(waits):
        if wait:
            log.warning("Gemini retry in %ss (%s)", wait, last_err[:180])
            sleep(wait)
        resp = do_post(
            url,
            headers=gemini_headers(api_key, base_url),
            json=payload,
            timeout=180,
        )
        if resp.status_code == 200:
            data = resp.json()
            cands = data.get("candidates") or []
            if not cands:
                raise RuntimeError("empty candidates")
            content_parts = cands[0].get("content", {}).get("parts") or []
            text = "".join(p.get("text", "") for p in content_parts if isinstance(p, dict))
            if not text.strip():
                raise RuntimeError("empty text")
            return text, parse_gemini_usage(data)
        body = resp.text or ""
        if is_quota_response(resp.status_code, body):
            msg = GEMINI_CREDITS_MSG if studio else "Недостаточно средств на ProxyAPI (HTTP 402). Пополните баланс."
            set_quota_alert(msg)
            raise LlmQuotaError(msg)
        if resp.status_code in _RETRY_STATUSES:
            last_err = f"HTTP {resp.status_code} {body[:200]}"
            log.warning("Gemini %s", last_err[:240])
            retry_s = _retry_after_seconds(resp, 0)
            if studio and resp.status_code == 429 and retry_s > 180:
                raise LlmRateLimitError(gemini_quota_wait_message(body, retry_s))
            if attempt + 1 < len(waits):
                waits[attempt + 1] = max(
                    waits[attempt + 1],
                    retry_s if retry_s else waits[attempt + 1],
                )
                if waits[attempt + 1] > 120:
                    waits[attempt + 1] = 120
            continue
        raise RuntimeError(f"HTTP {resp.status_code} {body[:300]}")
    if studio:
        wait_s = _retry_after_seconds(resp, 60) if resp is not None else 60
        body = (resp.text if resp is not None else "") or last_err
        raise LlmRateLimitError(gemini_quota_wait_message(body, wait_s))
    raise LlmRateLimitError(last_err or "Gemini rate limited")
