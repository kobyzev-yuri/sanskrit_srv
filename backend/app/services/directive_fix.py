"""Apply exact Devanagari wrong→right replacements from expert directives."""
from __future__ import annotations

import re

# BMP Devanagari + Vedic extensions (e.g. ꣳ U+A8F3)
_DEV = r"[\u0900-\u097F\uA8E0-\uA8FF\u1CD0-\u1CFF]+"

_PAIR_PATTERNS = [
    re.compile(
        rf"получил(?:ось|ся)\s*[«\"']?({_DEV})[»\"']?\s*исправь\s*на\s*[«\"']?({_DEV})[»\"']?",
        re.I,
    ),
    re.compile(
        rf"исправь\s*[«\"']?({_DEV})[»\"']?\s*на\s*[«\"']?({_DEV})[»\"']?",
        re.I,
    ),
    re.compile(
        rf"замени\s*[«\"']?({_DEV})[»\"']?\s*(?:на|→|->)\s*[«\"']?({_DEV})[»\"']?",
        re.I,
    ),
    re.compile(rf"({_DEV})\s*(?:→|->)\s*({_DEV})"),
]


def extract_replacements(directive: str) -> list[tuple[str, str]]:
    """Return (wrong, right) pairs mentioned in the directive."""
    text = (directive or "").strip()
    if not text:
        return []
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pat in _PAIR_PATTERNS:
        for m in pat.finditer(text):
            wrong, right = m.group(1), m.group(2)
            if wrong == right:
                continue
            key = (wrong, right)
            if key not in seen:
                seen.add(key)
                pairs.append(key)
    return pairs


def apply_directive_replacements(html: str, directive: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace exact wrong→right strings in HTML. Returns (new_html, applied_pairs)."""
    pairs = extract_replacements(directive)
    if not html or not pairs:
        return html or "", []
    out = html
    applied: list[tuple[str, str]] = []
    for wrong, right in pairs:
        if wrong in out:
            out = out.replace(wrong, right)
            applied.append((wrong, right))
    return out, applied
