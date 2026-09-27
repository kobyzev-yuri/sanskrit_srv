"""Insert IAST lines from a transliteration project into a Russian translation draft.

Target layout per line: Devanagari → IAST → Russian.
Works inside nested <div class="shloka"> / <footer> (leaf <p>/<h*>), not only
top-level article children.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s\|।॥\d०-९0-9\.\,\;\:\!\?\-\—\–\'\"“”‘’\(\)\[\]\{\}]+")
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


def merge_iast_into_translation(ru_html: str, iast_html: str) -> tuple[str, dict[str, int]]:
    """Insert missing IAST leaves after each Sanskrit leaf in the Russian draft."""
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
        }

    used: set[int] = set()
    cursor = 0
    inserted = 0
    skipped = 0
    unmatched = 0
    # (insert_at_offset, html) — apply from the end so offsets stay valid
    insertions: list[tuple[int, str]] = []

    for idx, leaf in enumerate(leaves):
        if not is_sa_leaf(leaf):
            continue
        nxt = leaves[idx + 1] if idx + 1 < len(leaves) else None
        if nxt is not None and is_iast_leaf(nxt):
            skipped += 1
            continue
        sa_norm = normalize_sa(visible_text(leaf.group(0)))
        iast_el, cursor = pick_iast(sa_norm, pairs, used, cursor=cursor)
        if iast_el:
            insertions.append((leaf.end(), "\n" + iast_el))
            inserted += 1
        else:
            unmatched += 1

    if not insertions:
        return src, {
            "inserted": inserted,
            "skipped_existing": skipped,
            "unmatched_sa": unmatched,
            "pairs": len(pairs),
            "used_pairs": len(used),
        }

    parts: list[str] = []
    pos = len(src)
    for offset, chunk in sorted(insertions, key=lambda x: x[0], reverse=True):
        parts.append(src[offset:pos])
        parts.append(chunk)
        pos = offset
    parts.append(src[:pos])
    parts.reverse()
    return "".join(parts), {
        "inserted": inserted,
        "skipped_existing": skipped,
        "unmatched_sa": unmatched,
        "pairs": len(pairs),
        "used_pairs": len(used),
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
