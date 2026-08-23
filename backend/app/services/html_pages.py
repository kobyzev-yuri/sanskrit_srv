"""Split uploaded HTML into page fragments."""
from __future__ import annotations

import re

_ARTICLE_RE = re.compile(r"<article\b[\s\S]*?</article>", re.IGNORECASE)


def split_html_pages(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    found = _ARTICLE_RE.findall(text)
    if found:
        return [a.strip() for a in found if a.strip()]
    return [text]
