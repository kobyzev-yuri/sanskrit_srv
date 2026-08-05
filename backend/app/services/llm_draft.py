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
1) text column width (narrow / medium / wide) and whether there are ONE or TWO vertical columns
2) title alignment (center / left)
3) line height / leading (tight dense print, normal, or loose/open)
4) body size vs headings (small body + larger title, or even)
5) ornaments / figures separate from the full-page plate
6) for lists / TOC: first and last item number in EACH column

Then encode that judgment in classes (do not write the judgment as visible text).

LAYOUT (line-by-line, book-like — mandatory):
- Emit content as a vertical sequence of block tags matching the printed lines top→bottom (and left column then right column if two columns).
- ONE printed line (or one half-verse line) → usually ONE <p> / <h1> / list row. Do not merge distant lines into one CSS row.
- Narrow column / short verse -> class="narrow" and/or class="shloka". Do NOT stretch full-width when the book is a narrow column.
- Two columns (TOC etc.): restore BOTH columns completely through the last numbered line. No empty placeholder rows.
- Titles: <h1 class="sa centered"> or <p class="running-head sa">. Match center/left as on the scan.
- Indent first line of a couplet continuation -> class="indent". Verse stack -> class="shloka sa". Prose -> <p class="sa">.
- Header line: separate blocks only — <p class="page-num">, <p class="running-head sa">, optional section tag. Place them in reading order as on the scan (often page number, then book title, then section in brackets).
- Page number / imprint: <p class="page-num"> or <footer class="sa"> where the scan has them.
- Tables stay <table>. 
- Two-column TOC / अनुक्रमणिका / विषयसूची (critical):
  Use ONE <table class="toc sa"> for the WHOLE page body with exactly FOUR cells per data row:
  <td>left title</td><td>left page</td><td>right title</td><td>right page</td>.
  Pair row i of the left printed column with row i of the right printed column.
  Section headings (e.g. विविध श्लोकाः) stay inside that same 4-column table (as a cell), never start a second 2-column table underneath — that collapses the layout to one column.
  Empty <td></td> only when that side has no more lines. Include every numbered line through the end of BOTH columns.

- Prefer class="compact" on <article> when the page is dense.

FORBIDDEN (never invent browser layout hacks):
- No inline style="..." (no flex, float, clear, grid, width%, justify-content, etc.).
- No <div style=...>, no display:flex / float:left / float:right headers.
- Layout only via the allowed classes below.

TYPOGRAPHY:
- Wrap in <article class="page-style TYPE LH" lang="sa"> where TYPE is type-sm|type-md|type-lg and LH is lh-tight|lh-normal|lh-loose.
- Headings may add type-lg; body follows the article default.
- Approximate metal type with size + leading classes and Noto Serif Devanagari (we cannot load the scan font).

TEXT:
- Capture ALL readable text through the last line of every column. Preserve Devanagari. class="sa" lang="sa".
- Do NOT invent text. Do NOT leave blank rows where the scan still has numbers/text.
- If a previous draft used flex/float or truncated a column, replace with clean class-based line-by-line HTML from the scan.

DEVANAGARI CONJUNCTS (critical — do not "guess" from Latin habits):
- Stacked vertical ङ् + ग on the scan is the ligature ङ्ग (ṅga). Encode as ङ्ग (U+0919 VIRAMA U+0917), NEVER as ज्ञ (jña).
- Classic false reading at line/page end: scan अङ्गानां / अङ्गानाम् (ṅga ligature) misread as अज्ञानां (jña). Also watch for truncated last words/lines — always finish the paragraph as on the scan.
- Soft/palatal ñ is ञ; retroflex ṇ is ण; dental n is न; velar ṅ is ङ. Do not swap them.
- ज्ञ = ज् + ञ (jña). ङ्ग = ङ् + ग (ṅga). They look different; prefer the scan, not a familiar wrong word.
- Final consonant: if the scan shows explicit म् / न् etc., keep the virama form; use anusvāra ं only when the scan has the dot.
- Half-forms and conjuncts (त्र, प्र, क्ष, त्त, ङ्ग, ज्ञ, …) must stay as proper Unicode conjuncts so the font can draw the ligature.

FIGURES:
- Ornaments/diagrams (not the full page): <figure class="scan-crop" data-box="x,y,w,h"></figure> with fractions 0-1.
- Embedded figures if listed: <img data-fig="N" alt="..." />.
- Never crop the entire page as a figure.

Return ONLY a raw HTML fragment starting with <article …> (no markdown, no commentary, no English step-by-step).
Do not narrate your judgment — encode it in classes and emit HTML immediately.
Allowed classes: page-style, type-sm, type-md, type-lg, lh-tight, lh-normal, lh-loose, narrow, indent, shloka, centered, compact, running-head, page-num, footer, scan-crop, page-figure, toc, data-box, data-fig.
"""

GARBAGE_ANYWHERE = re.compile(
    r"Let's look at the image|Wait, the|No markdown|```html|"
    r"\bthinking\b|The user wants me to|I need to follow these steps|"
    r"Judge the scan|Address specific constraints|silently judge|"
    r"Conflict with the horizontal header|strict interpretation",
    re.I,
)
AVAGRAHA_RUN = re.compile(r"ऽ{4,}")


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
    low = text.lower()
    start = low.find("<article")
    if start < 0:
        start = low.find("<div")
    if start < 0:
        start = low.find("<p")
    if start > 0:
        text = text[start:]
    end = max(text.rfind("</article>"), text.rfind("</div>"))
    if end > 0:
        close = text.find(">", end)
        if close > 0:
            text = text[: close + 1]
    return text.strip()


def looks_like_page_html(html: str) -> bool:
    h = (html or "").strip()
    if not h or h.count("<") < 2:
        return False
    if GARBAGE_ANYWHERE.search(h):
        return False
    if AVAGRAHA_RUN.search(h):
        return False
    low = h.lower()
    return "<article" in low or ("<p" in low and "class=" in low)


def validate_html(html: str) -> str:
    cleaned = extract_html_only(html)
    if GARBAGE_ANYWHERE.search(cleaned):
        raise ValueError("response looks like reasoning, not HTML")
    if AVAGRAHA_RUN.search(cleaned):
        raise ValueError("response has broken avagraha run")
    if cleaned.count("<") < 2:
        raise ValueError("response has too few HTML tags")
    if "<article" not in cleaned.lower() and cleaned.count("<p") < 2:
        raise ValueError("response is not a page HTML fragment")
    dev = sum(1 for c in cleaned if "\u0900" <= c <= "\u097f")
    if dev < 8 and "page-style" not in cleaned:
        raise ValueError("response lacks Devanagari page content")
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
    if current_html and looks_like_page_html(current_html):
        parts.append(
            "Current draft HTML (may be incomplete/wrong — fix from the scan; "
            "strip any style=/flex/float and rebuild line-by-line with classes only):\n"
            + current_html.strip()
        )
    elif current_html and current_html.strip():
        parts.append(
            "Previous draft was invalid (reasoning/garbage). Ignore it and produce fresh HTML from the scan."
        )
    if directive and directive.strip():
        parts.append("Editor directive (follow carefully):\n" + directive.strip())
    else:
        parts.append(
            "Produce a complete layout-faithful draft for the whole page "
            "(line-by-line classes only; no inline CSS). "
            "Output ONLY the HTML fragment — no English commentary, no step lists."
        )
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
