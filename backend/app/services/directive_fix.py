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

# «в WORD вместо OLD вставь NEW» → replace OLD with NEW inside WORD
_INPLACE_WORD_FIRST = re.compile(
    rf"в\s*[«\"']?({_DEV})[»\"']?\s*вместо\s*[«\"']?({_DEV})[»\"']?\s*"
    rf"(?:вставь|поставь|замени(?:ть)?\s*на)\s*[«\"']?({_DEV})[»\"']?",
    re.I,
)
# «вместо OLD вставь NEW в WORD»
_INPLACE_WORD_LAST = re.compile(
    rf"вместо\s*[«\"']?({_DEV})[»\"']?\s*(?:вставь|поставь|замени(?:ть)?\s*на)\s*"
    rf"[«\"']?({_DEV})[»\"']?\s*(?:в|в\s+слове)\s*[«\"']?({_DEV})[»\"']?",
    re.I,
)


def _add_pair(pairs: list[tuple[str, str]], seen: set[tuple[str, str]], wrong: str, right: str) -> None:
    if not wrong or wrong == right:
        return
    key = (wrong, right)
    if key not in seen:
        seen.add(key)
        pairs.append(key)


def extract_replacements(directive: str) -> list[tuple[str, str]]:
    """Return (wrong, right) pairs mentioned in the directive."""
    text = (directive or "").strip()
    if not text:
        return []
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for pat in _PAIR_PATTERNS:
        for m in pat.finditer(text):
            _add_pair(pairs, seen, m.group(1), m.group(2))

    for m in _INPLACE_WORD_FIRST.finditer(text):
        word, old, new = m.group(1), m.group(2), m.group(3)
        if old in word:
            _add_pair(pairs, seen, word, word.replace(old, new))

    for m in _INPLACE_WORD_LAST.finditer(text):
        old, new, word = m.group(1), m.group(2), m.group(3)
        if old in word:
            _add_pair(pairs, seen, word, word.replace(old, new))

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
