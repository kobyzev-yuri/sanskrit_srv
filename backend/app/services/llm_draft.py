"""Draft / revise page HTML from scan via vision LLM (Gemini AI Studio / OpenRouter / ProxyAPI)."""
from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.config import get_settings
from app.services.llm_status import LlmQuotaError, LlmRateLimitError, is_quota_response, set_quota_alert
from app.services.openrouter_ox import (
    TASK_DRAFT,
    apply_ox_chat_options,
    openrouter_headers,
    post_openrouter_chat,
)
from app.services.llm_route import (
    effective_openrouter_base_url,
    effective_openrouter_key,
    effective_proxyapi_key,
    model_plan,
    model_plan_primary_only,
    require_keys_for_plan,
)
from app.services.llm_usage import parse_anthropic_usage, parse_openai_usage

BASE_PROMPT = """You restore a Devanagari scan page (Sanskrit, Hindi, or mixed) into an HTML fragment.

The IMAGE is ground truth for TEXT, LAYOUT, and TYPOGRAPHY. Match the book visual style as closely as HTML allows.

LANGUAGE (silent first judgment — encode in lang=, do not narrate):
- Pure Sanskrit → <article … lang="sa">; body/verse blocks class="sa" lang="sa".
- Pure Hindi → <article … lang="hi">; prose class="sa" lang="hi" (class sa = Devanagari font, not “Sanskrit-only”).
- Mixed pages are common: Sanskrit śloka / sūtra / mantra + Hindi ṭīkā / vyākhyā / translation.
  - Mark Sanskrit lines: class="sa shloka" (or plain sa) lang="sa".
  - Mark Hindi commentary/prose: class="sa" lang="hi".
  - Article lang= the dominant language of the page (usually "hi" if commentary fills most of the leaf, else "sa").
- Do NOT Sanskritize Hindi wording, and do NOT translate or modernize Sanskrit into Hindi.
- Hindi nukta letters (क़ ख़ ग़ ज़ ड़ ढ़ फ़) must be kept when the scan shows them.

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
- Wrap in <article class="page-style TYPE LH" lang="sa|hi"> where TYPE is type-sm|type-md|type-lg and LH is lh-tight|lh-normal|lh-loose.
- Headings may add type-lg; body follows the article default.
- Approximate metal type with size + leading classes and Noto Serif Devanagari (we cannot load the scan font).

TEXT:
- Capture ALL readable text through the last line of every column. Preserve Devanagari; set lang="sa" or lang="hi" per block as above.
- Do NOT invent text. Do NOT leave blank rows where the scan still has numbers/text.
- If a previous draft used flex/float or truncated a column, replace with clean class-based line-by-line HTML from the scan.

FIDELITY OVER MEMORY (critical — this is diplomatic transcription, not editing):
- Encode ONLY what is printed on THIS scan. Do NOT substitute a "standard", "correct", dictionary, GRETIL, or remembered mantra spelling.
- Familiar hymns are the highest risk: you will "know" गणपतिं हवामहे / ॐ गणानां त्वा… — if the plate prints गणपतिग्ं / ऋतग्ं / ग्ं / etc., keep the plate, even when it looks non-Classical.
- Same rule for rare or "wrong-looking" words (ऋतग्ं-, odd sandhi, old orthography): leave them; do not silently normalize.
- Never insert an extra akṣara to "repair" sandhi or word division. Classic false add: plate मीश्वरस्सर्व- / मीश्वर-स्सर्व- misread as मीश्वर-रस्सर्व- (spurious र before स्). Hyphens only where the plate has them; no invented र / रा / य / व for sandhi.
- Never "fix" nasalization either way. ग्ं / गुंँ / गँ / म् / ं / ँ / ꣳ are different signs — copy the one printed on THIS syllable.
- Hard ban both directions:
  - Do NOT rewrite plate …ग्ं… into classical …ं (wrong: गणपतिग्ं → गणपतिं, ऋतग्ं → ऋतं).
  - Do NOT rewrite ordinary plate …ं… into …ग्ं / गुंँ / ँ / ꣳ. Most anusvāras stay plain ं. Gum is only where the plate shows half-ग + ं (or another marked gum form).
- Illustrations only (not a global replace rule; omit ॑/॒ in output per tone policy below):
  - if plate has …गणपतिग्ं… → keep …गणपतिग्ं…; if plate has …गणपतिं… → keep …गणपतिं…
  - if plate has …ऋतग्ं… → keep …ऋतग्ं…; if plate has …ऋतं… → keep …ऋतं…

DEVANAGARI CONJUNCTS (critical — do not "guess" from Latin habits):
- Stacked vertical ङ् + ग on the scan is the ligature ङ्ग (ṅga). Encode as ङ्ग (U+0919 VIRAMA U+0917), NEVER as ज्ञ (jña).
- Classic false reading at line/page end: scan अङ्गानां / अङ्गानाम् (ṅga ligature) misread as अज्ञानां (jña). Also watch for truncated last words/lines — always finish the paragraph as on the scan.
- Soft/palatal ñ is ञ; retroflex ṇ is ण; dental n is न; velar ṅ is ङ. Do not swap them.
- ज्ञ = ज् + ञ (jña). ङ्ग = ङ् + ग (ṅga). They look different; prefer the scan, not a familiar wrong word.
- Final consonant / nasal: if the scan shows explicit म् / न् etc., keep the virama form; use anusvāra ं (or Vedic ꣳ) only when the scan has that mark.
- Do not invent an extra ग after anusvāra: wrong एकहंगसः vs correct एकहंसः / एकहꣳसः (haṃsaḥ — anusvāra on ह, then स, no ङ्ग).
- Vedic "gum" (half-ग + ं) only when visible as such on the plate. Do not sprinkle ग्ं onto every nasal; do not "upgrade" plain ं to gum from hymn memory.
- Half-forms and conjuncts (त्र, प्र, क्ष, त्त, ङ्ग, ज्ञ, …) must stay as proper Unicode conjuncts so the font can draw the ligature.

VEDIC SVARA / TONE MARKS — OMIT ENTIRELY:
- Do NOT emit ॑ (U+0951) or ॒ (U+0952) at all. False positives are too frequent; bare akṣaras without tones are required.
- Ignore anudātta underlines and udātta/svarita strokes on the scan for encoding purposes — transcribe letters/matras/nasals only.
- Do NOT use HTML <u>, CSS underline, underscore _, or Latin diacritics as stand-ins for tones.
- Also never emit: U+0346, U+0304, U+0305, U+0323, U+0303, U+0307, or other combining tone fakes.

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
    r"Conflict with the horizontal header|strict interpretation|"
    r"Let me analyze the source|Keep Devanagari exactly as in source|"
    r"TEMPLATE interlinear|SOURCE HTML:",
    re.I,
)
AVAGRAHA_RUN = re.compile(r"ऽ{4,}")
PAGE_BATCH_MARK = re.compile(
    r"(?:\*{0,2})={2,}\s*PAGE\s+(\d+)\s*={2,}(?:\*{0,2})",
    re.I,
)
_ARTICLE_BLOCK = re.compile(r"<article\b.*?</article>", re.I | re.S)


def image_to_jpeg_b64(path: Path, max_px: int = 2048) -> str:
    """JPEG for vision; 2048px helps fine Vedic accent strokes survive compression."""
    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# Fake Latin combining marks models invent instead of real svara.
_FAKE_SVARA_RE = re.compile(
    "["
    + re.escape(
        "".join(
            (
                "\u0346",  # COMBINING BRIDGE ABOVE
                "\u0304",  # MACRON
                "\u0305",  # OVERLINE
                "\u0307",  # DOT ABOVE
                "\u0303",  # TILDE
                "\u0310",  # CANDRABINDU-like
                "\u0323",  # DOT BELOW
                "\u0331",  # MACRON BELOW
                "\u0320",  # MINUS BELOW
            )
        )
    )
    + "]"
)
# Real Devanagari stress signs — stripped from LLM drafts by default.
_REAL_SVARA_RE = re.compile("[\u0951\u0952]")


def normalize_vedic_marks(html: str) -> str:
    """Strip fake Latin tone diacritics (safe for saved editor HTML)."""
    return _FAKE_SVARA_RE.sub("", html)


def strip_vedic_svara(html: str) -> str:
    """Remove real ॑/॒ and fake tone marks — default for LLM drafts."""
    return _REAL_SVARA_RE.sub("", normalize_vedic_marks(html))


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


def validate_html(html: str, *, strip_svara: bool = True) -> str:
    cleaned = extract_html_only(html)
    cleaned = strip_vedic_svara(cleaned) if strip_svara else normalize_vedic_marks(cleaned)
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


def split_batch_page_html(text: str, page_nos: list[int] | None = None) -> dict[int, str]:
    """Parse ===PAGE n=== blocks from a multi-page digitize response."""
    raw = text or ""
    marks = list(PAGE_BATCH_MARK.finditer(raw))
    out: dict[int, str] = {}
    if marks:
        for i, m in enumerate(marks):
            no = int(m.group(1))
            start = m.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
            chunk = raw[start:end].strip()
            if chunk:
                out[no] = chunk
        return out
    nos = [int(n) for n in (page_nos or [])]
    if not nos:
        return {}
    articles = _ARTICLE_BLOCK.findall(raw)
    if len(articles) == len(nos):
        return dict(zip(nos, [a.strip() for a in articles], strict=True))
    return {}


def digitize_batch_size_for_plan(plan: dict[str, list[str]] | None = None) -> int:
    """How many consecutive scan pages to send in one vision call.

    1M input easily holds 10 JPEGs. The limit is *output*: dense Devanagari HTML
    is 3–8k tokens/page. Flash often stops after 1–2 pages if asked for 6.
    GLM-5V stays at 1 (weaker vision + thinking).
    """
    from app.config import get_settings as _gs

    cap = max(1, int(getattr(_gs(), "digitize_batch_pages", 6) or 6))
    cap = min(cap, 8)
    plan = plan if plan is not None else model_plan_primary_only()
    or_models = " ".join(plan.get("openrouter") or []).lower()
    if "glm" in or_models:
        return 1
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


def consecutive_page_runs(page_nos: list[int], *, max_n: int) -> list[list[int]]:
    """Group increasing page numbers into consecutive runs of at most max_n."""
    if max_n <= 1:
        return [[n] for n in page_nos]
    runs: list[list[int]] = []
    buf: list[int] = []
    for n in page_nos:
        if buf and n == buf[-1] + 1 and len(buf) < max_n:
            buf.append(n)
        else:
            if buf:
                runs.append(buf)
            buf = [n]
    if buf:
        runs.append(buf)
    return runs


def revise_from_scan(
    scan_path: Path,
    *,
    page_no: int,
    current_html: str | None = None,
    directive: str | None = None,
    available_figures: list[dict] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Return (html, model_used, usage). Tries the admin LLM route (OpenRouter by default).

    usage keys: network, model, prompt_tokens, completion_tokens, total_tokens, usage_raw
    """
    settings = get_settings()
    plan = model_plan_primary_only()
    require_keys_for_plan(plan)
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
            "strip any style=/flex/float and rebuild line-by-line with classes only). "
            "Distrust dictionary spellings in this draft: re-read nasals from the scan "
            "(keep गणपतिग्ं / ऋतग्ं if printed, do not keep गणपतिं / ऋतं from memory). "
            "Strip any ॑/॒ — tones are not used in drafts:\n"
            + current_html.strip()
        )
    elif current_html and current_html.strip():
        parts.append(
            "Previous draft was invalid (reasoning/garbage). Ignore it and produce fresh HTML from the scan."
        )
    if directive and directive.strip():
        parts.append(
            "Editor directive (follow carefully; apply every character change named):\n"
            + directive.strip()
            + "\n"
            "If the directive says to replace one akṣara inside a word "
            "(e.g. в इत्युपैष्यहं вместо ष вставь म → इत्युपैम्यहं), do exactly that "
            "substitution in the HTML; keep the rest of the page."
        )
    else:
        parts.append(
            "Produce a complete layout-faithful draft for the whole page "
            "(line-by-line classes only; no inline CSS). "
            "Output ONLY the HTML fragment — no English commentary, no step lists."
        )
    user_text = "\n\n".join(parts)
    # Keep ॑/॒ only when the editor directive explicitly names them.
    strip_svara = not bool(
        directive and re.search(r"[॒॑]|тон", directive, re.I)
    )

    errors: list[str] = []
    anthropic_models = plan["anthropic"]
    gemini_models = plan["gemini"]
    openai_models = plan["openai"]
    openrouter_models = plan.get("openrouter") or []

    for model in _uniq(openrouter_models):
        try:
            html, usage = _call_openrouter(
                effective_openrouter_key(),
                effective_openrouter_base_url(),
                model,
                user_text,
                image_b64,
            )
            usage = {**usage, "network": (
                "haimaker" if "haimaker.ai" in (effective_openrouter_base_url() or "") else "openrouter"
            ), "model": model}
            return validate_html(html, strip_svara=strip_svara), f"openrouter:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openrouter:{model}: {exc}")

    for model in _uniq(anthropic_models):
        try:
            html, usage = _call_anthropic(
                effective_proxyapi_key(),
                settings.anthropic_base_url,
                model,
                user_text,
                image_b64,
            )
            usage = {**usage, "network": "anthropic", "model": model}
            return validate_html(html, strip_svara=strip_svara), f"anthropic:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"anthropic:{model}: {exc}")

    for model in _uniq(gemini_models):
        try:
            html, usage = _call_gemini(model, user_text, image_b64)
            usage = {**usage, "network": "gemini", "model": model}
            return validate_html(html, strip_svara=strip_svara), f"gemini:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gemini:{model}: {exc}")

    for model in _uniq(openai_models):
        try:
            html, usage = _call_openai(
                effective_proxyapi_key(), settings.openai_base_url, model, user_text, image_b64
            )
            usage = {**usage, "network": "openai", "model": model}
            return validate_html(html, strip_svara=strip_svara), f"openai:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openai:{model}: {exc}")

    raise RuntimeError("; ".join(errors[-6:]) or "all models failed")


def _draft_raw_from_images(
    user_text: str,
    images: list[str],
    *,
    max_tokens: int,
    timeout: float,
) -> tuple[str, str, dict[str, Any]]:
    settings = get_settings()
    plan = model_plan_primary_only()
    require_keys_for_plan(plan)
    errors: list[str] = []
    for model in _uniq(plan.get("openrouter") or []):
        try:
            html, usage = _call_openrouter(
                effective_openrouter_key(),
                effective_openrouter_base_url(),
                model,
                user_text,
                images,
            )
            usage = {
                **usage,
                "network": (
                    "haimaker"
                    if "haimaker.ai" in (effective_openrouter_base_url() or "")
                    else "openrouter"
                ),
                "model": model,
            }
            return html, f"openrouter:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openrouter:{model}: {exc}")
    for model in _uniq(plan.get("anthropic") or []):
        try:
            html, usage = _call_anthropic(
                effective_proxyapi_key(),
                settings.anthropic_base_url,
                model,
                user_text,
                images,
                max_tokens=max_tokens,
            )
            usage = {**usage, "network": "anthropic", "model": model}
            return html, f"anthropic:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"anthropic:{model}: {exc}")
    for model in _uniq(plan.get("gemini") or []):
        try:
            html, usage = _call_gemini(
                model,
                user_text,
                images,
                max_output_tokens=max_tokens,
                timeout=timeout,
            )
            usage = {**usage, "network": "gemini", "model": model}
            return html, f"gemini:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gemini:{model}: {exc}")
    for model in _uniq(plan.get("openai") or []):
        try:
            html, usage = _call_openai(
                effective_proxyapi_key(),
                settings.openai_base_url,
                model,
                user_text,
                images,
                max_tokens=max_tokens,
            )
            usage = {**usage, "network": "openai", "model": model}
            return html, f"openai:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openai:{model}: {exc}")
    raise RuntimeError("; ".join(errors[-6:]) or "all models failed")


def revise_from_scans(
    pages: list[dict[str, Any]],
) -> tuple[dict[int, str], str, dict[str, Any]]:
    """Digitize one or more consecutive scan pages. Returns (html_by_page_no, model, usage)."""
    if not pages:
        raise ValueError("no pages")
    if len(pages) == 1:
        p = pages[0]
        html, model, usage = revise_from_scan(
            Path(p["scan_path"]),
            page_no=int(p["page_no"]),
            current_html=p.get("current_html"),
            directive=p.get("directive")
            or "Сделай полный HTML-черновик всей страницы по скану, сохранив стиль и компоновку книги.",
            available_figures=p.get("available_figures"),
        )
        return {int(p["page_no"]): html}, model, usage

    nos = [int(p["page_no"]) for p in pages]
    images = [image_to_jpeg_b64(Path(p["scan_path"])) for p in pages]
    parts = [
        BASE_PROMPT,
        (
            f"You are given {len(pages)} consecutive scan images, in order: "
            f"pages {nos[0]}–{nos[-1]}. Image 1 is page {nos[0]}, image 2 is page {nos[1] if len(nos) > 1 else nos[0]}, and so on."
        ),
        "Use neighbors for hyphenation, running headers, and verse continuation. "
        "Do not copy body text from one page onto another.",
        f"You MUST emit a block for EVERY page {nos[0]}–{nos[-1]} — do not stop after the first. "
        "A short or blank leaf still gets its own block.",
        "Output ONLY labeled HTML. For every page emit exactly these two lines of structure:",
        "===PAGE N===",
        "<article …>…</article>",
        "No commentary, markdown fences, or extra headings outside those blocks.",
    ]
    for p in pages:
        no = int(p["page_no"])
        figs = p.get("available_figures") or []
        if figs:
            fig_s = ", ".join(
                f"data-fig={f['index']} ({f.get('w')}×{f.get('h')})" for f in figs
            )
            parts.append(f"Page {no} embedded figures: {fig_s}")
        cur = p.get("current_html")
        if cur and looks_like_page_html(cur) and len(cur.strip()) > 200:
            parts.append(f"Current draft for page {no} (fix from the scan):\n{cur.strip()}")
    user_text = "\n\n".join(parts)
    n = len(pages)
    max_tokens = min(32768, max(8192, 8000 * n))
    timeout = 240.0 if n > 2 else 180.0
    raw, model, usage = _draft_raw_from_images(
        user_text, images, max_tokens=max_tokens, timeout=timeout
    )
    blocks = split_batch_page_html(raw, page_nos=nos)
    out: dict[int, str] = {}
    errors: list[str] = []
    for no in nos:
        chunk = blocks.get(no)
        if not chunk:
            errors.append(f"page {no} missing from batch")
            continue
        try:
            out[no] = validate_html(chunk, strip_svara=True)
        except ValueError as exc:
            errors.append(f"page {no}: {exc}")
    if not out:
        raise RuntimeError("; ".join(errors) or "batch digitize produced no pages")
    return out, model, usage


def run_vision_prompt(
    scan_path: Path,
    user_text: str,
    *,
    opus_only: bool = False,
    primary_only: bool = True,
) -> tuple[str, str, dict[str, Any]]:
    """Call the admin-selected LLM route with scan + text prompt. Returns (raw_text, model_id, usage).

    primary_only=True (default) — only the backoffice primary, no silent Gemini/OpenAI fallback.
    opus_only=True — deprecated alias: Claude only (ProxyAPI).
    """
    settings = get_settings()
    if opus_only:
        opus = (settings.anthropic_model or "").strip() or "claude-opus-5"
        plan = {"openrouter": [], "anthropic": [opus], "gemini": [], "openai": []}
    elif primary_only:
        plan = model_plan_primary_only()
    else:
        plan = model_plan()
    require_keys_for_plan(plan)
    if not scan_path.exists():
        raise FileNotFoundError(f"scan missing: {scan_path}")

    image_b64 = image_to_jpeg_b64(scan_path)
    errors: list[str] = []

    for model in _uniq(plan.get("openrouter") or []):
        try:
            text, usage = _call_openrouter(
                effective_openrouter_key(),
                effective_openrouter_base_url(),
                model,
                user_text,
                image_b64,
            )
            usage = {**usage, "network": (
                "haimaker" if "haimaker.ai" in (effective_openrouter_base_url() or "") else "openrouter"
            ), "model": model}
            return text, f"openrouter:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openrouter:{model}: {exc}")

    for model in _uniq(plan["anthropic"]):
        try:
            text, usage = _call_anthropic(
                effective_proxyapi_key(),
                settings.anthropic_base_url,
                model,
                user_text,
                image_b64,
            )
            usage = {**usage, "network": "anthropic", "model": model}
            return text, f"anthropic:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"anthropic:{model}: {exc}")

    if opus_only or primary_only:
        raise RuntimeError(
            "Смысловая проверка: вызов основной модели не удался: "
            + ("; ".join(errors[-4:]) or "unknown error")
        )

    for model in _uniq(plan["gemini"]):
        try:
            text, usage = _call_gemini(model, user_text, image_b64)
            usage = {**usage, "network": "gemini", "model": model}
            return text, f"gemini:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gemini:{model}: {exc}")

    for model in _uniq(plan["openai"]):
        try:
            text, usage = _call_openai(
                effective_proxyapi_key(), settings.openai_base_url, model, user_text, image_b64
            )
            usage = {**usage, "network": "openai", "model": model}
            return text, f"openai:{model}", usage
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openai:{model}: {exc}")

    raise RuntimeError("; ".join(errors[-6:]) or "all models failed")


def _require_keys_for_plan(_settings: Any, plan: dict[str, list[str]]) -> None:
    require_keys_for_plan(plan)


def _openai_message_text(message: dict[str, Any] | None) -> str:
    """Prefer `content`. If empty, take HTML from `reasoning` — never raw chain-of-thought."""
    msg = message or {}
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = [
            str(p.get("text") or "")
            for p in content
            if isinstance(p, dict) and p.get("type") in (None, "text")
        ]
        joined = "".join(parts)
        if joined.strip():
            return joined
    # GLM-5.3 (Haimaker) often leaves content empty and puts the answer in reasoning_content.
    for key in ("reasoning_content", "reasoning"):
        reasoning = msg.get(key)
        if not (isinstance(reasoning, str) and reasoning.strip()):
            continue
        extracted = extract_html_only(reasoning)
        if looks_like_page_html(extracted):
            return extracted
        if "<article" in extracted.lower() and not GARBAGE_ANYWHERE.search(extracted):
            return extracted
        stripped = reasoning.strip()
        if stripped.startswith("{") or '"suggestions"' in stripped:
            return stripped
    return ""


def _image_list(image_b64: str | list[str]) -> list[str]:
    if isinstance(image_b64, list):
        return [x for x in image_b64 if x]
    return [image_b64] if image_b64 else []


def _call_openrouter(
    api_key: str, base_url: str, model: str, user_text: str, image_b64: str | list[str]
) -> tuple[str, dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for blob in _image_list(image_b64):
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{blob}"}}
        )
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }
    apply_ox_chat_options(payload, model, task=TASK_DRAFT)
    data = post_openrouter_chat(
        url, headers=openrouter_headers(api_key), payload=payload, timeout=300
    )
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("empty choices")
    text = _openai_message_text(choices[0].get("message") if isinstance(choices[0], dict) else None)
    if not str(text).strip():
        raise RuntimeError("empty text from OpenRouter (reasoning-only?)")
    return text, parse_openai_usage(data)


def _uniq(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _call_anthropic(
    api_key: str,
    base_url: str,
    model: str,
    user_text: str,
    image_b64: str | list[str],
    *,
    max_tokens: int = 8192,
) -> tuple[str, dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/v1/messages"
    images = _image_list(image_b64)
    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": blob},
        }
        for blob in images
    ]
    content.append({"type": "text", "text": user_text})
    # Opus 5+: no temperature (deprecated); thinking off so we get a text/JSON block.
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max(1024, int(max_tokens)),
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": content}],
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
    # Older Claude ids may reject "thinking"; retry once without it.
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
    text = "".join(
        b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    )
    if not text.strip():
        # Last resort: some gateways put JSON in thinking/signature payloads.
        text = "".join(
            b.get("text", "") or b.get("thinking", "")
            for b in blocks
            if isinstance(b, dict)
        )
    if not text.strip():
        raise RuntimeError("empty text from Anthropic (thinking-only response?)")
    return text, parse_anthropic_usage(data)


def _call_gemini(
    model: str,
    user_text: str,
    image_b64: str | list[str],
    *,
    max_output_tokens: int | None = None,
    timeout: float = 180,
) -> tuple[str, dict[str, Any]]:
    from app.services.gemini_client import GEMINI_MAX_OUTPUT_TOKENS, generate_gemini_content

    parts: list[dict[str, Any]] = [{"text": user_text}]
    for blob in _image_list(image_b64):
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": blob}})
    return generate_gemini_content(
        model=model,
        parts=parts,
        max_output_tokens=int(max_output_tokens or GEMINI_MAX_OUTPUT_TOKENS),
        timeout=timeout,
    )


def _call_openai(
    api_key: str,
    base_url: str,
    model: str,
    user_text: str,
    image_b64: str | list[str],
    *,
    max_tokens: int = 8192,
) -> tuple[str, dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for blob in _image_list(image_b64):
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{blob}"}}
        )
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": max(1024, int(max_tokens)),
        "messages": [{"role": "user", "content": content}],
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
