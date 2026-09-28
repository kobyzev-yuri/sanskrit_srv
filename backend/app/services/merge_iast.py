"""Insert IAST lines from a transliteration project into a Russian translation draft.

Target layout per line: Devanagari → IAST → Russian.
Works inside nested <div class="shloka"> / <footer> (leaf <p>/<h*>).

When the Russian draft keeps several pādas in one <p class="sa"> joined by
<br>, each pāda still gets its own IAST paragraph from the IAST project.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s\|।॥\d०-९0-9\.\,\;\:\!\?\-\—\–\'\"“”‘’\(\)\[\]\{\}]+")
_BR_SPLIT = re.compile(r"<br\s*/?>", re.IGNORECASE)
# Leaf text blocks — Sanskrit/IAST/RU lines are almost always these tags.
_LEAF_RE = re.compile(
    r"<(p|h[1-6]|li)\b([^>]*)>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)


def normalize_sa(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "")
    t = _PUNCT.sub("", t)
    return t


def visible_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"&nbsp;", " ", text, flags=re.I)
    text = re.sub(r"&[a-z]+;", " ", text, flags=re.I)
    text = re.sub(r"&#\d+;", " ", text)
    return _WS.sub(" ", text).strip()


def _classes_from_attrs(attrs: str) -> set[str]:
    m = re.search(r'\bclass=["\']([^"\']+)["\']', attrs or "", re.I)
    if not m:
        return set()
    return set(m.group(1).lower().split())


def leaf_classes(match: re.Match[str]) -> set[str]:
    return _classes_from_attrs(match.group(2))


def is_iast_leaf(match: re.Match[str]) -> bool:
    return "iast" in leaf_classes(match)


def is_ru_leaf(match: re.Match[str]) -> bool:
    cls = leaf_classes(match)
    return "ru" in cls and "iast" not in cls


def is_sa_leaf(match: re.Match[str]) -> bool:
    cls = leaf_classes(match)
    if "iast" in cls:
        return False
    if "ru" in cls and "sa" not in cls:
        return False
    if "sa" in cls or "shloka" in cls:
        return True
    text = visible_text(match.group(0))
    if not text:
        return False
    deva = sum(1 for ch in text if "\u0900" <= ch <= "\u097F")
    return deva >= max(3, len(text) // 5)


def iter_leaves(html: str) -> list[re.Match[str]]:
    return list(_LEAF_RE.finditer(html or ""))


def sa_line_texts(leaf: re.Match[str]) -> list[str]:
    """Devanagari lines inside one leaf (split on <br> / newlines)."""
    inner = leaf.group(3) or ""
    chunks = _BR_SPLIT.split(inner)
    lines: list[str] = []
    for chunk in chunks:
        for part in re.split(r"[\r\n]+", chunk):
            text = visible_text(part)
            if text:
                lines.append(text)
    if not lines:
        whole = visible_text(leaf.group(0))
        if whole:
            lines.append(whole)
    return lines


def extract_sa_iast_pairs(iast_html: str) -> list[tuple[str, str]]:
    """Ordered (normalized_sa, iast_element_html) from leaf sa→iast siblings."""
    leaves = iter_leaves(iast_html)
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(leaves):
        cur = leaves[i]
        nxt = leaves[i + 1] if i + 1 < len(leaves) else None
        if is_sa_leaf(cur) and nxt is not None and is_iast_leaf(nxt):
            pairs.append((normalize_sa(visible_text(cur.group(0))), nxt.group(0).strip()))
            i += 2
            continue
        i += 1
    return pairs


def _score(sa_norm: str, key: str) -> float:
    if not sa_norm or not key:
        return 0.0
    if sa_norm == key:
        return 1.0
    if sa_norm in key or key in sa_norm:
        shorter = min(len(sa_norm), len(key))
        longer = max(len(sa_norm), len(key))
        return 0.88 + 0.1 * (shorter / longer)
    return SequenceMatcher(None, sa_norm, key).ratio()


def pick_iast(
    sa_norm: str,
    pairs: list[tuple[str, str]],
    used: set[int],
    *,
    cursor: int,
) -> tuple[str | None, int]:
    best_i = -1
    best_s = 0.0
    window = list(range(cursor, min(len(pairs), cursor + 10)))
    window += [i for i in range(len(pairs)) if i not in window]
    for i in window:
        if i in used:
            continue
        key, html = pairs[i]
        sc = _score(sa_norm, key)
        if i == cursor:
            sc += 0.08
        elif abs(i - cursor) <= 2:
            sc += 0.03
        if sc > best_s:
            best_s = sc
            best_i = i
    if best_i < 0 or best_s < 0.58:
        return None, cursor
    used.add(best_i)
    return pairs[best_i][1], best_i + 1


def _collect_iasts_for_lines(
    lines: list[str],
    pairs: list[tuple[str, str]],
    used: set[int],
    cursor: int,
) -> tuple[list[str], int, int]:
    """Return (iast_htmls, new_cursor, unmatched_line_count)."""
    iasts: list[str] = []
    unmatched = 0
    for line in lines:
        el, cursor = pick_iast(normalize_sa(line), pairs, used, cursor=cursor)
        if el:
            iasts.append(el)
        else:
            unmatched += 1
    return iasts, cursor, unmatched


def merge_iast_into_translation(ru_html: str, iast_html: str) -> tuple[str, dict[str, int]]:
    """Insert / complete IAST leaves after each Sanskrit leaf in the Russian draft.

    Multi-line <p class="sa"> (pādas joined by <br>) get one IAST <p> per pāda.
    Incomplete prior merges (only the first/last IAST) are replaced with the full set.
    """
    pairs = extract_sa_iast_pairs(iast_html)
    src = ru_html or ""
    leaves = iter_leaves(src)
    if not leaves:
        return src, {
            "inserted": 0,
            "skipped_existing": 0,
            "unmatched_sa": 0,
            "pairs": len(pairs),
            "used_pairs": 0,
            "replaced": 0,
        }

    used: set[int] = set()
    cursor = 0
    inserted = 0
    skipped = 0
    unmatched = 0
    replaced = 0
    # (start, end, replacement) — apply from the end
    edits: list[tuple[int, int, str]] = []

    for idx, leaf in enumerate(leaves):
        if not is_sa_leaf(leaf):
            continue

        lines = sa_line_texts(leaf)
        # How many consecutive IAST leaves already follow this sa?
        j = idx + 1
        while j < len(leaves) and is_iast_leaf(leaves[j]):
            j += 1
        existing = j - (idx + 1)
        span_start = leaf.end()
        span_end = leaves[idx + 1].start() if existing else leaf.end()
        if existing:
            span_end = leaves[j - 1].end()

        # Already complete for this sa block.
        if existing >= max(1, len(lines)) and len(lines) <= 1:
            skipped += 1
            continue
        if existing >= len(lines) and len(lines) > 1:
            skipped += 1
            continue

        iasts, cursor, miss = _collect_iasts_for_lines(lines, pairs, used, cursor)
        unmatched += miss
        if not iasts:
            if existing:
                skipped += 1
            else:
                unmatched += max(1, len(lines))
            continue

        chunk = "\n" + "\n".join(iasts)
        if existing:
            # Replace incomplete IAST run with the full set.
            edits.append((span_start, span_end, chunk))
            replaced += 1
            inserted += max(0, len(iasts) - existing)
        else:
            edits.append((span_start, span_start, chunk))
            inserted += len(iasts)

    if not edits:
        return src, {
            "inserted": inserted,
            "skipped_existing": skipped,
            "unmatched_sa": unmatched,
            "pairs": len(pairs),
            "used_pairs": len(used),
            "replaced": replaced,
        }

    parts: list[str] = []
    pos = len(src)
    for start, end, chunk in sorted(edits, key=lambda x: x[0], reverse=True):
        parts.append(src[end:pos])
        parts.append(chunk)
        pos = start
    parts.append(src[:pos])
    parts.reverse()
    return "".join(parts), {
        "inserted": inserted,
        "skipped_existing": skipped,
        "unmatched_sa": unmatched,
        "pairs": len(pairs),
        "used_pairs": len(used),
        "replaced": replaced,
    }


# --- backwards-compatible helpers used by older tests ---

def split_article_blocks(html: str) -> tuple[str, list[str], str]:
    """Legacy helper: return leaf outer-HTML list (prefix/suffix empty)."""
    leaves = [m.group(0) for m in iter_leaves(html)]
    return "", leaves, ""


def is_iast_block(html: str) -> bool:
    m = _LEAF_RE.search(html or "")
    return bool(m and is_iast_leaf(m))


def is_sa_block(html: str) -> bool:
    m = _LEAF_RE.search(html or "")
    return bool(m and is_sa_leaf(m))
