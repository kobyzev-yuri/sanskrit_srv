"""Draft / revise page HTML from scan via ProxyAPI vision (Gemini / OpenAI)."""
from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.config import get_settings
from app.services.llm_status import LlmQuotaError, is_quota_response, set_quota_alert
from app.services.llm_usage import parse_gemini_usage, parse_openai_usage

BASE_PROMPT = """You restore a Sanskrit scan page (manuscript or printed edition) into an HTML fragment.

The IMAGE is ground truth for TEXT, LAYOUT, and TYPOGRAPHY. Match the book visual style as closely as HTML allows.

First silently judge from the scan:
1) text column width (narrow / medium / wide)
2) title alignment (center / left)
3) line height / leading (tight dense print, normal, or loose/open)
4) body size vs headings (small body + larger title, or even)
5) ornaments / figures separate from the full-page plate

Then encode that judgment in classes (do not write the judgment as visible text).

LAYOUT:
- Narrow column / short verse lines -> class="narrow" and/or class="shloka" (centered). Do NOT stretch full-width when the book is a narrow column.
- Titles: match alignment. <h1 class="sa centered"> or <p class="running-head sa">. No fake left offset if the scan centers the title.
- Paragraphs: first-line indent -> class="indent"; verse -> class="shloka sa"; prose -> <p class="sa">.
- Vertical gaps: match the scan. Prefer class="compact" on wrapping <article> when the page is dense.
- Page number / imprint: <p class="page-num"> or <footer class="sa"> in the same place as the scan.
- Tables stay <table>.

TYPOGRAPHY (leading + relative size / "font feel"):
- Wrap the page in <article class="page-style TYPE LH" lang="sa"> where:
  TYPE is one of: type-sm | type-md | type-lg  (body size relative to a typical printed mantra book)
  LH is one of: lh-tight | lh-normal | lh-loose  (line height as on the scan)
- Headings may add type-lg; body follows the article default.
- Short airy verse lines: class="shloka lh-loose"; dense stacks: class="shloka lh-tight".
- We cannot load the exact metal type from the scan; approximate with size + leading classes and Noto Serif Devanagari.

TEXT:
- Capture ALL readable text. Preserve Devanagari. Use class="sa" lang="sa".
- Do NOT invent text.

FIGURES:
- Ornaments/diagrams (not the full page): <figure class="scan-crop" data-box="x,y,w,h"></figure> with fractions 0-1.
- Embedded figures if listed: <img data-fig="N" alt="..." />.
- Never crop the entire page as a figure.

Return ONLY a raw HTML fragment (no markdown, no commentary).
Classes: page-style, type-sm, type-md, type-lg, lh-tight, lh-normal, lh-loose, narrow, indent, shloka, centered, compact, running-head, page-num, footer, scan-crop, data-box, data-fig.
"""

GARBAGE_ANYWHERE = re.compile(
    r"Let's look at the image|Wait, the|No markdown|```html|thinking",
    re.I,
)


def image_to_jpeg_b64(path: Path, max_px: int = 1600) -> str:
    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def extract_html_only(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:html)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def validate_html(html: str) -> str:
    cleaned = extract_html_only(html)
    if GARBAGE_ANYWHERE.search(cleaned):
        raise ValueError("response looks like reasoning, not HTML")
    if cleaned.count("<") < 2:
        raise ValueError("response has too few HTML tags")
    return cleaned


def revise_from_scan(
    scan_path: Path,
    *,
    page_no: int,
    current_html: str | None = None,
    directive: str | None = None,
    available_figures: list[dict] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Return (html, model_used, usage). Tries Gemini then OpenAI fallbacks.

    usage keys: network, model, prompt_tokens, completion_tokens, total_tokens, usage_raw
    """
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY missing in server .env")
    if not scan_path.exists():
        raise FileNotFoundError(f"scan missing: {scan_path}")

    image_b64 = image_to_jpeg_b64(scan_path)
    parts = [
        BASE_PROMPT,
        f"Page number: {page_no}.",
        "First silently judge: column width, title alignment, verse vs prose, ornaments — then emit HTML that mirrors that.",
    ]
    if available_figures:
        figs = ", ".join(
            f"data-fig={f['index']} ({f.get('w')}×{f.get('h')})" for f in available_figures
        )
        parts.append(
            "Embedded figures available (use <img data-fig=\"N\" />): " + figs
        )
    if current_html and current_html.strip():
        parts.append("Current draft HTML (may be incomplete/wrong):\n" + current_html.strip())
    if directive and directive.strip():
        parts.append("Editor directive (follow carefully):\n" + directive.strip())
    else:
        parts.append("Produce a complete layout-faithful draft for the whole page.")
    user_text = "\n\n".join(parts)

    errors: list[str] = []
    gemini_models = [
        settings.gemini_model,
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-2.0-flash",
    ]
    openai_models = [settings.openai_model, "gpt-4o-mini", "gpt-4o"]

    for model in _uniq(gemini_models):
        try:
            html, usage = _call_gemini(
                settings.openai_api_key, settings.gemini_base_url, model, user_text, image_b64
            )
            usage = {**usage, "network": "gemini", "model": model}
            return validate_html(html), f"gemini:{model}", usage
        except LlmQuotaError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gemini:{model}: {exc}")

    for model in _uniq(openai_models):
        try:
            html, usage = _call_openai(
                settings.openai_api_key, settings.openai_base_url, model, user_text, image_b64
            )
            usage = {**usage, "network": "openai", "model": model}
            return validate_html(html), f"openai:{model}", usage
        except LlmQuotaError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openai:{model}: {exc}")

    raise RuntimeError("; ".join(errors[-6:]) or "all models failed")


def _uniq(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _call_gemini(
    api_key: str, base_url: str, model: str, user_text: str, image_b64: str
) -> tuple[str, dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": user_text},
                    {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                ],
            }
        ],
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


def _call_openai(
    api_key: str, base_url: str, model: str, user_text: str, image_b64: str
) -> tuple[str, dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 8192,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            }
        ],
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
    return choices[0].get("message", {}).get("content", ""), parse_openai_usage(data)
