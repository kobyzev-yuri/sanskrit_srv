"""Translate verified Sanskrit HTML → Russian HTML (text LLM, no scan)."""
from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.services.html_chunks import chunk_page_html, merge_translated_chunks, unwrap_article
from app.services.llm_draft import (
    _openai_message_text,
    _require_keys_for_plan,
    _uniq,
    extract_html_only,
)
from app.services.llm_route import model_plan
from app.services.llm_status import LlmQuotaError, is_quota_response, set_quota_alert
from app.services.llm_usage import parse_anthropic_usage, parse_gemini_usage, parse_openai_usage
from app.services.layout_assets import preserve_figure_srcs
from app.services.translation_style import build_translate_prompt


def validate_translation_html(html: str, *, source_html: str | None = None) -> str:
    cleaned = extract_html_only(html)
    if cleaned.count("<") < 2:
        raise ValueError("response has too few HTML tags")
    low = cleaned.lower()
    if "<article" not in low and cleaned.count("<p") < 2:
        raise ValueError("response is not a page HTML fragment")
    if source_html:
        cleaned = preserve_figure_srcs(source_html, cleaned)
    return cleaned


def _sum_usage(parts: list[dict[str, Any]]) -> dict[str, Any]:
    if not parts:
        return {}
    out: dict[str, Any] = dict(parts[-1])
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"):
        vals = [int(p.get(key) or 0) for p in parts]
        if any(vals):
            out[key] = sum(vals)
    return out


def translate_from_source(
    *,
    source_html: str,
    cfg: dict[str, Any],
    current_html: str | None = None,
    directive: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    if not (source_html or "").strip():
        raise ValueError("empty Sanskrit source")
    # Previous draft may already have corrupted figure UUIDs / blob: — repair before sending.
    if current_html:
        current_html = preserve_figure_srcs(source_html, current_html)

    chunks = chunk_page_html(source_html)
    if len(chunks) <= 1:
        prompt = build_translate_prompt(
            source_html=source_html,
            cfg=cfg,
            current_html=current_html,
            directive=directive,
        )
        raw, model, usage = run_text_prompt(prompt)
        html = validate_translation_html(raw, source_html=source_html)
        return html, model, usage

    # Large page: translate block packs separately, then merge (no previous draft — avoids mixing).
    article_open, _, _ = unwrap_article(source_html)
    parts_html: list[str] = []
    usages: list[dict[str, Any]] = []
    model = ""
    total = len(chunks)
    for i, chunk_src in enumerate(chunks, start=1):
        prompt = build_translate_prompt(
            source_html=chunk_src,
            cfg=cfg,
            current_html=None,
            directive=directive if i == 1 else None,
            chunk_index=i,
            chunk_total=total,
        )
        raw, model, usage = run_text_prompt(prompt)
        parts_html.append(extract_html_only(raw))
        usages.append(usage)
    merged = merge_translated_chunks(parts_html, article_open=article_open or None)
    html = validate_translation_html(merged, source_html=source_html)
    return html, model, _sum_usage(usages)


def run_text_prompt(user_text: str) -> tuple[str, str, dict[str, Any]]:
    settings = get_settings()
    plan = model_plan()
    _require_keys_for_plan(settings, plan)
    errors: list[str] = []

    for model in _uniq(plan.get("openrouter") or []):
        try:
            text, usage = _call_openrouter_text(
                settings.openrouter_api_key, settings.openrouter_base_url, model, user_text
            )
            usage = {**usage, "network": "openrouter", "model": model}
            return text, f"openrouter:{model}", usage
        except LlmQuotaError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openrouter:{model}: {exc}")

    for model in _uniq(plan.get("anthropic") or []):
        try:
            text, usage = _call_anthropic_text(
                settings.openai_api_key, settings.anthropic_base_url, model, user_text
            )
            usage = {**usage, "network": "anthropic", "model": model}
            return text, f"anthropic:{model}", usage
        except LlmQuotaError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"anthropic:{model}: {exc}")

    for model in _uniq(plan.get("gemini") or []):
        try:
            text, usage = _call_gemini_text(
                settings.openai_api_key, settings.gemini_base_url, model, user_text
            )
            usage = {**usage, "network": "gemini", "model": model}
            return text, f"gemini:{model}", usage
        except LlmQuotaError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gemini:{model}: {exc}")

    for model in _uniq(plan.get("openai") or []):
        try:
            text, usage = _call_openai_text(
                settings.openai_api_key, settings.openai_base_url, model, user_text
            )
            usage = {**usage, "network": "openai", "model": model}
            return text, f"openai:{model}", usage
        except LlmQuotaError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openai:{model}: {exc}")

    raise RuntimeError("; ".join(errors[-6:]) or "all models failed")


def _call_openrouter_text(api_key: str, base_url: str, model: str, user_text: str):
    settings = get_settings()
    url = f"{base_url.rstrip('/')}/chat/completions"
    limit = max(1024, int(settings.openrouter_max_tokens or 32768))
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": user_text}],
    }
    mid = (model or "").lower()
    if "ox-alpha" in mid or mid.startswith("stealth/"):
        payload["max_completion_tokens"] = limit
        payload["reasoning"] = {"enabled": True}
    else:
        payload["temperature"] = 0
        payload["max_tokens"] = limit
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": (settings.openrouter_http_referer or "https://sanskrit-srv.local"),
            "X-Title": (settings.openrouter_app_title or "sanskrit_srv"),
        },
        json=payload,
        timeout=300,
    )
    if resp.status_code != 200:
        body = resp.text[:400]
        if is_quota_response(resp.status_code, body):
            msg = "Лимит OpenRouter / оплата (HTTP 402)."
            set_quota_alert(msg)
            raise LlmQuotaError(msg)
        raise RuntimeError(f"HTTP {resp.status_code} {body[:300]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("empty choices")
    text = _openai_message_text(choices[0].get("message") if isinstance(choices[0], dict) else None)
    if not str(text).strip():
        raise RuntimeError("empty text from OpenRouter")
    return text, parse_openai_usage(data)


def _call_anthropic_text(api_key: str, base_url: str, model: str, user_text: str):
    url = f"{base_url.rstrip('/')}/v1/messages"
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 8192,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
    }
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=180,
    )
    if resp.status_code == 400 and "thinking" in (resp.text or "").lower():
        payload.pop("thinking", None)
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=180,
        )
    if resp.status_code != 200:
        body = resp.text[:400]
        if is_quota_response(resp.status_code, body):
            msg = "Недостаточно средств на ProxyAPI (HTTP 402). Пополните баланс."
            set_quota_alert(msg)
            raise LlmQuotaError(msg)
        raise RuntimeError(f"HTTP {resp.status_code} {body[:300]}")
    data = resp.json()
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    if not text.strip():
        raise RuntimeError("empty text from Anthropic")
    return text, parse_anthropic_usage(data)


def _call_gemini_text(api_key: str, base_url: str, model: str, user_text: str):
    url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 8192},
    }
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if resp.status_code != 200:
        body = resp.text[:400]
        if is_quota_response(resp.status_code, body):
            msg = "Недостаточно средств на ProxyAPI (HTTP 402). Пополните баланс."
            set_quota_alert(msg)
            raise LlmQuotaError(msg)
        raise RuntimeError(f"HTTP {resp.status_code} {body[:300]}")
    data = resp.json()
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError("empty candidates")
    parts = cands[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text.strip():
        raise RuntimeError("empty text")
    return text, parse_gemini_usage(data)


def _call_openai_text(api_key: str, base_url: str, model: str, user_text: str):
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": user_text}],
    }
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if resp.status_code != 200:
        body = resp.text[:400]
        if is_quota_response(resp.status_code, body):
            msg = "Недостаточно средств на ProxyAPI (HTTP 402). Пополните баланс."
            set_quota_alert(msg)
            raise LlmQuotaError(msg)
        raise RuntimeError(f"HTTP {resp.status_code} {body[:300]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("empty choices")
    text = choices[0].get("message", {}).get("content", "")
    if not str(text).strip():
        raise RuntimeError("empty text")
    return text, parse_openai_usage(data)
