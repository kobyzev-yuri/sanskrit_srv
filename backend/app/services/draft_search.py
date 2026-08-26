"""Find a substring in project draft / source HTML (visible text)."""
from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"(?is)<script[^>]*>.*?</script>")
STYLE_RE = re.compile(r"(?is)<style[^>]*>.*?</style>")
WS_RE = re.compile(r"\s+")


def html_to_text(raw: str | None) -> str:
    text = SCRIPT_RE.sub(" ", raw or "")
    text = STYLE_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def _fold_pair(text: str) -> tuple[str, str]:
    """Return (display_text, search_haystack). Indices match when casefold keeps length."""
    nfc = _norm(text)
    folded = nfc.casefold()
    if len(folded) == len(nfc):
        return nfc, folded
    return nfc, nfc


def iter_matches(text: str, query: str, *, limit: int = 8) -> list[tuple[int, int]]:
    if not query or not text:
        return []
    src, hay = _fold_pair(text)
    _qn, needle = _fold_pair(query)
    if not needle:
        return []
    out: list[tuple[int, int]] = []
    start = 0
    span = len(_norm(query))
    while len(out) < limit:
        i = hay.find(needle, start)
        if i < 0:
            break
        out.append((i, i + span))
        start = i + max(len(needle), 1)
    return out


def snippet_for(text: str, start: int, end: int, *, radius: int = 46) -> str:
    a = max(0, start - radius)
    b = min(len(text), end + radius)
    chunk = text[a:b]
    if a > 0:
        chunk = "…" + chunk.lstrip()
    if b < len(text):
        chunk = chunk.rstrip() + "…"
    return chunk


def search_html(raw: str | None, query: str, *, field: str, limit: int = 3) -> dict[str, Any] | None:
    text = html_to_text(raw)
    spans = iter_matches(text, query, limit=99)
    if not spans:
        return None
    return {
        "field": field,
        "count": len(spans),
        "snippets": [snippet_for(text, a, b) for a, b in spans[:limit]],
    }


def search_pages(pages: list[Any], query: str, *, include_source: bool = False) -> dict[str, Any]:
    q = _norm(query).strip()
    hits: list[dict[str, Any]] = []
    page_hits = 0
    total_matches = 0
    if len(q) < 1 or len(q) > 200:
        return {"query": q, "page_hits": 0, "total_matches": 0, "hits": []}
    for page in pages:
        fields = []
        draft = search_html(getattr(page, "current_html", None), q, field="draft")
        if draft:
            fields.append(draft)
        if include_source:
            src = search_html(getattr(page, "source_html", None), q, field="source")
            if src:
                fields.append(src)
        if not fields:
            continue
        page_hits += 1
        count = sum(int(f["count"]) for f in fields)
        total_matches += count
        snippets: list[str] = []
        for f in fields:
            prefix = "источник: " if f["field"] == "source" else ""
            for sn in f["snippets"]:
                snippets.append(prefix + sn)
        hits.append(
            {
                "page_id": str(page.id),
                "page_no": int(page.page_no),
                "count": count,
                "fields": [f["field"] for f in fields],
                "snippets": snippets[:4],
            }
        )
        if page_hits >= 80:
            break
    return {
        "query": q,
        "page_hits": page_hits,
        "total_matches": total_matches,
        "hits": hits,
        "truncated": page_hits >= 80,
    }
