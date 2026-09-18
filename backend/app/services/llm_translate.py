"""Translate verified Sanskrit HTML → Russian HTML (text LLM, no scan)."""
from __future__ import annotations

import re
from typing import Any, Callable

import httpx

from app.config import get_settings
from app.services.html_chunks import chunk_page_html, merge_translated_chunks, unwrap_article
from app.services.llm_draft import (
    GARBAGE_ANYWHERE,
    _openai_message_text,
    _require_keys_for_plan,
    _uniq,
    extract_html_only,
    split_batch_page_html,
)
from app.services.llm_route import (
    effective_openrouter_base_url,
    effective_openrouter_key,
    effective_proxyapi_key,
    gateway_batch_pages,
    model_plan_primary_only,
)
from app.services.llm_status import LlmQuotaError, LlmRateLimitError, is_quota_response, set_quota_alert
from app.services.llm_usage import parse_anthropic_usage, parse_openai_usage
from app.services.layout_assets import preserve_figure_srcs
from app.services.openrouter_ox import (
    TASK_TRANSLATE,
    apply_ox_chat_options,
    openrouter_headers,
    post_openrouter_chat,
)
from app.services.translation_style import (
    STYLE_IAST_BLOCK,
    build_translate_batch_messages,
    build_translate_messages,
    build_transliterate_batch_messages,
    build_transliterate_messages,
)


# Combined source chars for one translate call. Dense interlinear HTML is ~2× this in output.
TRANSLATE_BATCH_SOURCE_CAP = 22000


def translate_batch_size_for_plan(plan: dict[str, list[str]] | None = None) -> int:
    """How many consecutive source pages to translate in one text call.

    Output is the limiter (interlinear HTML). Flash often stops after 1–2 pages if asked for 6.
    """
    cap = max(1, int(getattr(get_settings(), "translate_batch_pages", 6) or 6))
    cap = min(cap, 8)
    plan = plan if plan is not None else model_plan_primary_only(text=True)
    or_models = [str(m) for m in (plan.get("openrouter") or []) if m]
    if or_models:
        return min(cap, gateway_batch_pages(or_models[0], vision=False))
    gemini = [str(m) for m in (plan.get("gemini") or [])]
    if gemini:
        blob = " ".join(gemini).lower()
        gem_n = 3 if "flash" in blob else 6
        return min(cap, gem_n)
    if plan.get("anthropic"):
        return min(cap, 4)
    if plan.get("openai"):
        return min(cap, 3)
    return 1


BLANK_RU_ARTICLE = '<article class="page-style" lang="ru">\n</article>'
BLANK_IAST_ARTICLE = '<article class="page-style" lang="sa-Latn">\n</article>'
_IAST_CLASS_RE = re.compile(r"""class=["'][^"']*\biast\b""", re.I)
_SA_CLASS_RE = re.compile(r"""class=["'][^"']*\b(?:sa|shloka)\b""", re.I)
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_IAST_MARK_RE = re.compile(r"[āīūṛṝḷḹṅñṭḍṇśṣḥṃĀĪŪṚṜḶḸṄÑṬḌṆŚṢḤṂ]|lang=[\"']sa-latn", re.I)


def visible_html_text(html: str) -> str:
    vis = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", vis).strip()


def page_too_large_for_batch(source_html: str) -> bool:
    """True if this page is already split into intra-page LLM chunks."""
    src = (source_html or "").strip()
    if not src:
        return True
    return len(chunk_page_html(src)) > 1


def pack_translate_runs(
    pages: list[Any],
    *,
    max_n: int,
    source_cap: int = TRANSLATE_BATCH_SOURCE_CAP,
) -> list[list[Any]]:
    """Group consecutive pages; isolate oversized leaves; cap combined source size."""
    if max_n <= 1:
        return [[p] for p in pages]
    runs: list[list[Any]] = []
    buf: list[Any] = []
    buf_chars = 0
    prev_no: int | None = None
    for p in pages:
        no = int(getattr(p, "page_no"))
        src = (getattr(p, "source_html", None) or "").strip()
        solo = page_too_large_for_batch(src)
        if solo:
            if buf:
                runs.append(buf)
                buf = []
                buf_chars = 0
            runs.append([p])
            prev_no = no
            continue
        can_join = (
            buf
            and prev_no is not None
            and no == prev_no + 1
            and len(buf) < max_n
            and buf_chars + len(src) <= source_cap
        )
        if can_join:
            buf.append(p)
            buf_chars += len(src)
        else:
            if buf:
                runs.append(buf)
            buf = [p]
            buf_chars = len(src)
        prev_no = no
    if buf:
        runs.append(buf)
    return runs


def validate_translation_html(html: str, *, source_html: str | None = None) -> str:
    # Extract first: ox-alpha often writes a short plan, then the real <article>.
    cleaned = extract_html_only(html)
    if GARBAGE_ANYWHERE.search(cleaned):
        raise ValueError("response looks like reasoning, not HTML")
    if cleaned.count("<") < 2:
        raise ValueError("response has too few HTML tags")
    low = cleaned.lower()
    if "<article" not in low:
        raise ValueError("response is not a page HTML fragment")
    visible = re.sub(r"<[^>]+>", " ", cleaned)
    visible = re.sub(r"\s+", " ", visible).strip()
    if len(visible) < 12:
        if source_html is not None and not visible_html_text(source_html):
            return cleaned
        raise ValueError("empty translation body")
    cyr = sum(1 for c in cleaned if "\u0400" <= c <= "\u04ff")
    if cyr < 8:
        raise ValueError("response lacks Russian translation")
    if source_html:
        cleaned = preserve_figure_srcs(source_html, cleaned)
    return cleaned


def looks_like_translation_html(html: str, source_html: str | None = None) -> bool:
    try:
        validate_translation_html(html, source_html=source_html)
        return True
    except ValueError:
        return False


def validate_transliteration_html(
    html: str,
    *,
    source_html: str | None = None,
    style: str | None = None,
) -> str:
    cleaned = extract_html_only(html)
    if GARBAGE_ANYWHERE.search(cleaned):
        raise ValueError("response looks like reasoning, not HTML")
    if cleaned.count("<") < 2:
        raise ValueError("response has too few HTML tags")
    low = cleaned.lower()
    if "<article" not in low:
        raise ValueError("response is not a page HTML fragment")
    visible = re.sub(r"<[^>]+>", " ", cleaned)
    visible = re.sub(r"\s+", " ", visible).strip()
    if len(visible) < 12:
        if source_html is not None and not visible_html_text(source_html):
            return cleaned
        raise ValueError("empty transliteration body")
    has_iast = bool(_IAST_CLASS_RE.search(cleaned) or _IAST_MARK_RE.search(cleaned))
    if not has_iast:
        raise ValueError("response lacks IAST transliteration")
    kind = (style or STYLE_IAST_BLOCK).strip().lower()
    if kind == STYLE_IAST_BLOCK:
        if not (_SA_CLASS_RE.search(cleaned) or _DEVANAGARI_RE.search(cleaned)):
            raise ValueError("response lacks Devanagari source lines")
    if source_html:
        cleaned = preserve_figure_srcs(source_html, cleaned)
    return cleaned


def looks_like_transliteration_html(
    html: str,
    source_html: str | None = None,
    *,
    style: str | None = None,
) -> bool:
    try:
        validate_transliteration_html(html, source_html=source_html, style=style)
        return True
    except ValueError:
        return False


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
    on_chunk: Callable[[int, int, str, dict[str, Any]], None] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    if not (source_html or "").strip():
        raise ValueError("empty Sanskrit source")
    # Previous draft may already have corrupted figure UUIDs / blob: — repair before sending.
    if current_html:
        current_html = preserve_figure_srcs(source_html, current_html)

    chunks = chunk_page_html(source_html)
    if len(chunks) <= 1:
        system, user = build_translate_messages(
            source_html=source_html,
            cfg=cfg,
            current_html=current_html,
            directive=directive,
        )
        raw, model, usage = run_text_prompt(user, system=system)
        if on_chunk:
            on_chunk(1, 1, model, usage)
        html = validate_translation_html(raw, source_html=source_html)
        return html, model, usage

    # Large page: translate block packs separately, then merge (no previous draft — avoids mixing).
    article_open, _, _ = unwrap_article(source_html)
    parts_html: list[str] = []
    usages: list[dict[str, Any]] = []
    model = ""
    total = len(chunks)
    for i, chunk_src in enumerate(chunks, start=1):
        system, user = build_translate_messages(
            source_html=chunk_src,
            cfg=cfg,
            current_html=None,
            directive=directive if i == 1 else None,
            chunk_index=i,
            chunk_total=total,
        )
        raw, model, usage = run_text_prompt(user, system=system)
        parts_html.append(extract_html_only(raw))
        usages.append(usage)
        if on_chunk:
            on_chunk(i, total, model, usage)
    merged = merge_translated_chunks(parts_html, article_open=article_open or None)
    html = validate_translation_html(merged, source_html=source_html)
    return html, model, _sum_usage(usages)


def transliterate_from_source(
    *,
    source_html: str,
    cfg: dict[str, Any],
    current_html: str | None = None,
    directive: str | None = None,
    on_chunk: Callable[[int, int, str, dict[str, Any]], None] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    if not (source_html or "").strip():
        raise ValueError("empty Sanskrit source")
    if current_html:
        current_html = preserve_figure_srcs(source_html, current_html)
    style = str(cfg.get("style") or STYLE_IAST_BLOCK)

    chunks = chunk_page_html(source_html)
    if len(chunks) <= 1:
        system, user = build_transliterate_messages(
            source_html=source_html,
            cfg=cfg,
            current_html=current_html,
            directive=directive,
        )
        raw, model, usage = run_text_prompt(user, system=system)
        if on_chunk:
            on_chunk(1, 1, model, usage)
        html = validate_transliteration_html(raw, source_html=source_html, style=style)
        return html, model, usage

    article_open, _, _ = unwrap_article(source_html)
    parts_html: list[str] = []
    usages: list[dict[str, Any]] = []
    model = ""
    total = len(chunks)
    for i, chunk_src in enumerate(chunks, start=1):
        system, user = build_transliterate_messages(
            source_html=chunk_src,
            cfg=cfg,
            current_html=None,
            directive=directive if i == 1 else None,
            chunk_index=i,
            chunk_total=total,
        )
        raw, model, usage = run_text_prompt(user, system=system)
        parts_html.append(extract_html_only(raw))
        usages.append(usage)
        if on_chunk:
            on_chunk(i, total, model, usage)
    merged = merge_translated_chunks(parts_html, article_open=article_open or None)
    html = validate_transliteration_html(merged, source_html=source_html, style=style)
    return html, model, _sum_usage(usages)


def transliterate_from_sources(
    pages: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
) -> tuple[dict[int, str], str, dict[str, Any]]:
    if not pages:
        raise ValueError("no pages")
    style = str(cfg.get("style") or STYLE_IAST_BLOCK)
    if len(pages) == 1:
        p = pages[0]
        html, model, usage = transliterate_from_source(
            source_html=p["source_html"],
            cfg=cfg,
            current_html=None,
            directive=None,
        )
        return {int(p["page_no"]): html}, model, usage

    labeled: list[tuple[int, str]] = []
    for p in pages:
        no = int(p["page_no"])
        src = (p.get("source_html") or "").strip()
        if not src:
            raise ValueError(f"empty Sanskrit source for page {no}")
        labeled.append((no, src))
    nos = [n for n, _ in labeled]
    system, user = build_transliterate_batch_messages(pages=labeled, cfg=cfg)
    n = len(labeled)
    max_tokens = min(32768, max(8192, 8000 * n))
    timeout = 240.0 if n > 2 else 180.0
    plan = model_plan_primary_only(text=True)
    think = " ".join(plan.get("openrouter") or []).lower()
    if "ox-alpha" in think or "stealth/" in think:
        max_tokens = min(max_tokens, 8192)
        timeout = 180.0
    raw, model, usage = run_text_prompt(user, system=system, max_tokens=max_tokens, timeout=timeout)
    blocks = split_batch_page_html(raw, page_nos=nos)
    out: dict[int, str] = {}
    errors: list[str] = []
    src_by_no = {n: s for n, s in labeled}
    for no in nos:
        chunk = blocks.get(no)
        if not chunk:
            errors.append(f"page {no} missing from batch")
            continue
        try:
            out[no] = validate_transliteration_html(chunk, source_html=src_by_no[no], style=style)
        except ValueError as exc:
            errors.append(f"page {no}: {exc}")
    if not out:
        raise RuntimeError("; ".join(errors) or "batch transliterate produced no pages")
    return out, model, usage


def translate_from_sources(
    pages: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
) -> tuple[dict[int, str], str, dict[str, Any]]:
    """Translate one or more consecutive source pages. Returns (html_by_page_no, model, usage)."""
    if not pages:
        raise ValueError("no pages")
    if len(pages) == 1:
        p = pages[0]
        html, model, usage = translate_from_source(
            source_html=p["source_html"],
            cfg=cfg,
            current_html=None,
            directive=None,
        )
        return {int(p["page_no"]): html}, model, usage

    labeled: list[tuple[int, str]] = []
    for p in pages:
        no = int(p["page_no"])
        src = (p.get("source_html") or "").strip()
        if not src:
            raise ValueError(f"empty Sanskrit source for page {no}")
        labeled.append((no, src))
    nos = [n for n, _ in labeled]
    system, user = build_translate_batch_messages(pages=labeled, cfg=cfg)
    n = len(labeled)
    max_tokens = min(32768, max(8192, 8000 * n))
    timeout = 240.0 if n > 2 else 180.0
    plan = model_plan_primary_only(text=True)
    think = " ".join(plan.get("openrouter") or []).lower()
    if "ox-alpha" in think or "stealth/" in think:
        max_tokens = min(max_tokens, 8192)
        timeout = 180.0
    raw, model, usage = run_text_prompt(user, system=system, max_tokens=max_tokens, timeout=timeout)
    blocks = split_batch_page_html(raw, page_nos=nos)
    out: dict[int, str] = {}
    errors: list[str] = []
    src_by_no = {n: s for n, s in labeled}
    for no in nos:
        chunk = blocks.get(no)
        if not chunk:
            errors.append(f"page {no} missing from batch")
            continue
        try:
            out[no] = validate_translation_html(chunk, source_html=src_by_no[no])
        except ValueError as exc:
            errors.append(f"page {no}: {exc}")
    if not out:
        raise RuntimeError("; ".join(errors) or "batch translate produced no pages")
    return out, model, usage


def run_text_prompt(
    user_text: str,
    *,
    system: str | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> tuple[str, str, dict[str, Any]]:
    settings = get_settings()
    plan = model_plan_primary_only(text=True)
    _require_keys_for_plan(settings, plan)
    errors: list[str] = []
    to = float(timeout or 180)
    sys = (system or "").strip() or None

    for model in _uniq(plan.get("openrouter") or []):
        try:
            text, usage = _call_openrouter_text(
                effective_openrouter_key(),
                effective_openrouter_base_url(),
                model,
                user_text,
                system=sys,
                max_tokens=max_tokens,
                timeout=to,
            )
            usage = {**usage, "network": "openrouter", "model": model}
            return text, f"openrouter:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openrouter:{model}: {exc}")

    for model in _uniq(plan.get("anthropic") or []):
        try:
            text, usage = _call_anthropic_text(
                effective_proxyapi_key(),
                settings.anthropic_base_url,
                model,
                user_text,
                system=sys,
                max_tokens=max_tokens,
                timeout=to,
            )
            usage = {**usage, "network": "anthropic", "model": model}
            return text, f"anthropic:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"anthropic:{model}: {exc}")

    for model in _uniq(plan.get("gemini") or []):
        try:
            text, usage = _call_gemini_text(
                model, user_text, system=sys, max_tokens=max_tokens, timeout=to
            )
            usage = {**usage, "network": "gemini", "model": model}
            return text, f"gemini:{model}", usage
        except LlmQuotaError:
            raise
        except LlmRateLimitError as exc:
            errors.append(f"gemini:{model}: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gemini:{model}: {exc}")

    for model in _uniq(plan.get("openai") or []):
        try:
            text, usage = _call_openai_text(
                effective_proxyapi_key(),
                settings.openai_base_url,
                model,
                user_text,
                system=sys,
                max_tokens=max_tokens,
                timeout=to,
            )
            usage = {**usage, "network": "openai", "model": model}
            return text, f"openai:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openai:{model}: {exc}")

    raise RuntimeError("; ".join(errors[-6:]) or "all models failed")


def _openai_style_messages(user_text: str, system: str | None) -> list[dict[str, str]]:
    msgs: list[dict[str, str]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": user_text})
    return msgs


def _call_openrouter_text(
    api_key: str,
    base_url: str,
    model: str,
    user_text: str,
    *,
    system: str | None = None,
    max_tokens: int | None = None,
    timeout: float = 180,
):
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": _openai_style_messages(user_text, system),
    }
    apply_ox_chat_options(payload, model, task=TASK_TRANSLATE, max_tokens=max_tokens)
    data = post_openrouter_chat(
        url, headers=openrouter_headers(api_key), payload=payload, timeout=timeout
    )
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("empty choices")
    text = _openai_message_text(choices[0].get("message") if isinstance(choices[0], dict) else None)
    if not str(text).strip():
        raise RuntimeError("empty text from LLM gateway")
    return text, parse_openai_usage(data)


def _call_anthropic_text(
    api_key: str,
    base_url: str,
    model: str,
    user_text: str,
    *,
    system: str | None = None,
    max_tokens: int | None = None,
    timeout: float = 180,
):
    url = f"{base_url.rstrip('/')}/v1/messages"
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max(1024, int(max_tokens or 8192)),
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
    }
    if system:
        payload["system"] = system
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
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
            timeout=timeout,
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


def _call_gemini_text(
    model: str,
    user_text: str,
    *,
    system: str | None = None,
    max_tokens: int | None = None,
    timeout: float = 180,
):
    from app.services.gemini_client import generate_gemini_content

    kwargs: dict[str, Any] = {"timeout": timeout}
    if max_tokens:
        kwargs["max_output_tokens"] = max(1024, int(max_tokens))
    if system:
        kwargs["system"] = system
    return generate_gemini_content(
        model=model,
        parts=[{"text": user_text}],
        **kwargs,
    )


def _call_openai_text(
    api_key: str,
    base_url: str,
    model: str,
    user_text: str,
    *,
    system: str | None = None,
    max_tokens: int | None = None,
    timeout: float = 180,
):
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": max(1024, int(max_tokens or 8192)),
        "messages": _openai_style_messages(user_text, system),
    }
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
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
