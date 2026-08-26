"""Second-pass sense check: propose word fixes without auto-applying (except batch high)."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Page
from app.services.llm_draft import run_vision_prompt
from app.services.storage import project_dir

PROOFREAD_PROMPT = """You are a Sanskrit/Hindi Devanagari proofreader for diplomatic transcription.

You receive: (1) the page SCAN image (ground truth), (2) the current draft HTML.

Task: find places where the DRAFT likely lost meaning vs the scan — e.g. truncated word endings
(missing र / स् / matra), broken conjuncts, nonsense tokens — NOT “improve” classical spelling.

STRICT RULES:
- Prefer the SCAN over dictionary / memory / GRETIL. Do NOT “fix” गणपतिग्ं→गणपतिं or similar.
- Do NOT suggest tone marks ॑/॒.
- Only suggest when you can see the better reading on the scan OR the draft token is clearly truncated/nonsensical and the scan supports the fix.
- `wrong` must be an EXACT contiguous substring that appears in the draft HTML (copy-paste from the draft).
- `right` is the corrected Devanagari (or short phrase) to replace that substring once.
- Prefer whole-word / akṣara-group replacements over single ambiguous letters when possible.
- Skip layout/CSS issues. Skip already-correct text.
- Max 20 suggestions. If nothing suspicious, return an empty list.
- Output must be the JSON object only (type=text). No preamble.

Return ONLY valid JSON (no markdown fences) with this shape:
{"suggestions":[{"id":"1","wrong":"…","right":"…","reason":"short reason in Russian or English","severity":"high|medium|low"}]}
"""

TRANSLATE_PROOFREAD_PROMPT = """You proofread a Russian translation of a Sanskrit (or Hindi) book page.

You receive:
1) SOURCE HTML — verified Devanagari from the digitize project (left pane). OCR artifacts are still possible.
2) DRAFT HTML — current translation page (Sanskrit lines kept + Russian).
3) PREV page tail and NEXT page head — to check continuity across the page break.

Look for:

GROSS (severity=high, kind=incomplete, target=draft):
- A Sanskrit block (class containing "sa") that the template should follow with Russian (class="ru tr") but the Russian is missing, empty, or a stub («…», one word, cut mid-sentence).
- Russian that stops mid-śloka / mid-sentence.
Fix: `wrong` = the exact DRAFT HTML substring (usually the Sanskrit <p>…</p>); `right` = that same substring PLUS the completed <p class="ru tr" lang="ru">…</p> (and any missing Russian). Copy tags exactly.

JOIN (severity=high or medium, kind=join, target=draft):
- End of PREV translation and start of THIS draft do not continue one thought (cut sentence, duplicated fragment, missing object).
- End of THIS draft vs start of NEXT similarly.
Edit THIS draft only. `wrong`/`right` must be substrings of THIS DRAFT HTML.

SANSKRIT (severity=medium or low, kind=sanskrit, target=source or both):
- Likely OCR / copy errors in Devanagari (truncated ending, broken conjunct, nonsense token).
- Do NOT classicize Vedic spellings (ग्ं, ळ, rare sandhi). Do NOT add svara ॑/॒.
- `wrong` must be an exact substring of SOURCE and/or DRAFT.

SENSE (severity=medium or low, kind=sense, target=draft):
- Russian that contradicts the Sanskrit (wrong polarity, wrong name/deity, omitted clause).
- Do not restyle literary Russian when the meaning is fine.

STRICT RULES:
- `wrong` is an EXACT contiguous substring copied from the HTML you are correcting (DRAFT unless target=source).
- `right` replaces that substring once. Keep surrounding HTML/classes/figures unchanged.
- Skip CSS/layout. Skip already-correct text.
- Max 25 suggestions. Empty list if the page is fine.
- JSON object only. No markdown fences. Reasons in Russian, short.

Return ONLY valid JSON:
{"suggestions":[{"id":"1","wrong":"…","right":"…","reason":"…","severity":"high|medium|low","kind":"incomplete|join|sanskrit|sense","target":"draft|source|both"}]}
"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)
_ALLOWED_SEV = frozenset({"high", "medium", "low"})
_ALLOWED_KIND = frozenset({"incomplete", "join", "sanskrit", "sense", ""})
_ALLOWED_TARGET = frozenset({"draft", "source", "both"})


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty proofread response")
    m = _JSON_FENCE.search(raw)
    if m:
        raw = m.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in proofread response")
    return json.loads(raw[start : end + 1])


def clip_html(html: str | None, *, limit: int, from_end: bool = False) -> str:
    text = html or ""
    if len(text) <= limit:
        return text
    if from_end:
        return "…\n" + text[-limit:]
    return text[:limit] + "\n…"


def neighbor_html(db: Session, page: Page) -> dict[str, str]:
    prev = db.scalar(
        select(Page).where(Page.project_id == page.project_id, Page.page_no == page.page_no - 1)
    )
    nxt = db.scalar(
        select(Page).where(Page.project_id == page.project_id, Page.page_no == page.page_no + 1)
    )
    return {
        "prev_draft": (prev.current_html or "") if prev else "",
        "prev_source": (prev.source_html or "") if prev else "",
        "next_draft": (nxt.current_html or "") if nxt else "",
        "next_source": (nxt.source_html or "") if nxt else "",
    }


def _normalize_suggestions(
    raw_items: list[Any],
    *,
    draft_html: str,
    source_html: str = "",
    limit: int = 20,
) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for i, item in enumerate(raw_items[:limit], start=1):
        if not isinstance(item, dict):
            continue
        wrong = str(item.get("wrong") or "").strip()
        right = str(item.get("right") or "").strip()
        if not wrong or not right or wrong == right:
            continue
        target = str(item.get("target") or "draft").strip().lower()
        if target not in _ALLOWED_TARGET:
            target = "draft"
        in_draft = wrong in draft_html
        in_source = bool(source_html) and wrong in source_html
        if target == "source":
            if not in_source:
                if in_draft:
                    target = "draft"
                else:
                    continue
        elif target == "both":
            if not in_draft and not in_source:
                continue
        elif not in_draft:
            if in_source:
                target = "source"
            else:
                continue
        key = (wrong, right, target)
        if key in seen:
            continue
        seen.add(key)
        severity = str(item.get("severity") or "medium").strip().lower()
        if severity not in _ALLOWED_SEV:
            severity = "medium"
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in _ALLOWED_KIND:
            kind = ""
        suggestions.append(
            {
                "id": str(item.get("id") or i),
                "wrong": wrong,
                "right": right,
                "reason": str(item.get("reason") or "").strip()[:400],
                "severity": severity,
                "kind": kind,
                "target": target,
            }
        )
    return suggestions


def proofread_from_scan(
    scan_path: Path,
    *,
    page_no: int,
    current_html: str,
) -> tuple[list[dict[str, str]], str, dict[str, Any]]:
    """Return (suggestions, model_id, usage). Does not modify HTML."""
    html = (current_html or "").strip()
    if not html:
        raise ValueError("page has no HTML draft to proofread")

    user_text = "\n\n".join(
        [
            PROOFREAD_PROMPT,
            f"Page number: {page_no}.",
            "Draft HTML:\n" + html,
        ]
    )
    text, model, usage = run_vision_prompt(scan_path, user_text, primary_only=True)
    data = _extract_json(text)
    raw_items = data.get("suggestions") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        raise ValueError("proofread JSON missing suggestions[]")
    return _normalize_suggestions(raw_items, draft_html=html, limit=20), model, usage


def proofread_translation(
    *,
    page_no: int,
    source_html: str,
    current_html: str,
    prev_draft: str = "",
    prev_source: str = "",
    next_draft: str = "",
    next_source: str = "",
    style: str = "interlinear",
) -> tuple[list[dict[str, str]], str, dict[str, Any]]:
    """Sense-check Russian draft vs Sanskrit source (+ page-break neighbours)."""
    from app.services.llm_translate import run_text_prompt

    draft = (current_html or "").strip()
    source = (source_html or "").strip()
    if not draft:
        raise ValueError("page has no translation HTML to proofread")
    if not source:
        raise ValueError("page has no Sanskrit source HTML")

    parts = [
        TRANSLATE_PROOFREAD_PROMPT,
        f"Page number: {page_no}. Translation template: {style}.",
        "SOURCE HTML (verified Devanagari):\n" + clip_html(source, limit=22000),
        "DRAFT HTML (this page translation):\n" + clip_html(draft, limit=22000),
    ]
    if prev_draft or prev_source:
        parts.append(
            "PREVIOUS PAGE tail (do not edit; continuity only):\n"
            + "SOURCE END:\n"
            + clip_html(prev_source, limit=2500, from_end=True)
            + "\nDRAFT END:\n"
            + clip_html(prev_draft, limit=2500, from_end=True)
        )
    if next_draft or next_source:
        parts.append(
            "NEXT PAGE head (do not edit; continuity only):\n"
            + "SOURCE START:\n"
            + clip_html(next_source, limit=2500)
            + "\nDRAFT START:\n"
            + clip_html(next_draft, limit=2500)
        )
    text, model, usage = run_text_prompt("\n\n".join(parts))
    data = _extract_json(text)
    raw_items = data.get("suggestions") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        raise ValueError("proofread JSON missing suggestions[]")
    return (
        _normalize_suggestions(raw_items, draft_html=draft, source_html=source, limit=25),
        model,
        usage,
    )


def apply_proofread_suggestions(html: str, accepted: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    """Apply accepted wrong→right replacements (one occurrence each, longest wrong first)."""
    out = html or ""
    applied: list[dict[str, str]] = []
    ordered = sorted(
        (a for a in accepted if a.get("wrong") and a.get("right")),
        key=lambda a: len(a["wrong"]),
        reverse=True,
    )
    for item in ordered:
        wrong, right = item["wrong"], item["right"]
        if wrong not in out:
            continue
        out = out.replace(wrong, right, 1)
        applied.append(
            {
                "id": str(item.get("id") or ""),
                "wrong": wrong,
                "right": right,
                "target": str(item.get("target") or "draft"),
                "kind": str(item.get("kind") or ""),
                "severity": str(item.get("severity") or ""),
            }
        )
    return out, applied


def split_by_target(
    accepted: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Draft-bound items vs source-bound items (both → both lists)."""
    draft_items: list[dict[str, str]] = []
    source_items: list[dict[str, str]] = []
    for item in accepted:
        target = str(item.get("target") or "draft").strip().lower()
        if target in ("source", "both"):
            source_items.append(item)
        if target in ("draft", "both", ""):
            draft_items.append(item)
        elif target == "source":
            pass
    return draft_items, source_items


def gross_draft_items(suggestions: list[dict[str, str]]) -> list[dict[str, str]]:
    """High-severity translation holes that are safe to auto-apply on open pages."""
    out: list[dict[str, str]] = []
    for s in suggestions:
        if str(s.get("severity") or "") != "high":
            continue
        target = str(s.get("target") or "draft")
        kind = str(s.get("kind") or "")
        if target == "source" or kind == "sanskrit":
            continue
        out.append(s)
    return out


def remaining_after_apply(
    suggestions: list[dict[str, str]],
    applied: list[dict[str, str]],
) -> list[dict[str, str]]:
    done = {(a.get("wrong"), a.get("right"), a.get("target") or "draft") for a in applied}
    left: list[dict[str, str]] = []
    for s in suggestions:
        key = (s.get("wrong"), s.get("right"), s.get("target") or "draft")
        if key in done:
            continue
        left.append(s)
    return left


def proofread_store_path(project_id: uuid.UUID, page_id: uuid.UUID) -> Path:
    folder = project_dir(project_id) / "proofread"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{page_id}.json"


def save_page_proofread(
    project_id: uuid.UUID,
    page_id: uuid.UUID,
    *,
    suggestions: list[dict[str, str]],
    model: str = "",
    note: str = "",
    job_id: str | None = None,
) -> None:
    path = proofread_store_path(project_id, page_id)
    if not suggestions:
        if path.exists():
            path.unlink()
        return
    payload = {
        "suggestions": suggestions,
        "model": model,
        "note": note,
        "job_id": job_id,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_page_proofread(project_id: uuid.UUID, page_id: uuid.UUID) -> dict[str, Any] | None:
    path = proofread_store_path(project_id, page_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("suggestions")
    if not isinstance(items, list) or not items:
        return None
    return data


def proofread_counts(project_id: uuid.UUID) -> dict[str, int]:
    folder = project_dir(project_id) / "proofread"
    if not folder.exists():
        return {}
    out: dict[str, int] = {}
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        n = len(data.get("suggestions") or []) if isinstance(data, dict) else 0
        if n:
            out[path.stem] = n
    return out
