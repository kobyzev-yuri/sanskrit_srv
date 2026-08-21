"""Second-pass sense check: propose word fixes without auto-applying."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.services.llm_draft import run_vision_prompt

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


_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty proofread response")
    m = _JSON_FENCE.search(raw)
    if m:
        raw = m.group(1).strip()
    # Prefer outermost object
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in proofread response")
    return json.loads(raw[start : end + 1])


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

    suggestions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(raw_items[:20], start=1):
        if not isinstance(item, dict):
            continue
        wrong = str(item.get("wrong") or "").strip()
        right = str(item.get("right") or "").strip()
        if not wrong or not right or wrong == right:
            continue
        if wrong not in html:
            continue
        key = (wrong, right)
        if key in seen:
            continue
        seen.add(key)
        severity = str(item.get("severity") or "medium").strip().lower()
        if severity not in ("high", "medium", "low"):
            severity = "medium"
        suggestions.append(
            {
                "id": str(item.get("id") or i),
                "wrong": wrong,
                "right": right,
                "reason": str(item.get("reason") or "").strip()[:400],
                "severity": severity,
            }
        )
    return suggestions, model, usage


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
        applied.append({"id": str(item.get("id") or ""), "wrong": wrong, "right": right})
    return out, applied
