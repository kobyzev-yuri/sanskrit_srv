"""Insert IAST lines from a transliteration project into a Russian translation draft.

Target layout per block: Devanagari (sa) → IAST → Russian (ru).
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from html.parser import HTMLParser


_BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "li", "div", "figure", "footer", "blockquote"})
_VOID = frozenset({"img", "br", "hr", "meta", "link", "input", "source", "wbr"})
_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s\|।॥\d०-९0-9\.\,\;\:\!\?\-\—\–\'\"“”‘’\(\)\[\]\{\}]+")


def normalize_sa(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "")
    t = _PUNCT.sub("", t)
    return t


def _cls(attrs: list[tuple[str, str | None]]) -> list[str]:
    for k, v in attrs:
        if k.lower() == "class" and v:
            return v.lower().split()
    return []


def _attr_str(attrs: list[tuple[str, str | None]]) -> str:
    parts: list[str] = []
    for k, v in attrs:
        if v is None:
            parts.append(k)
        else:
            parts.append(f'{k}="{v}"')
    return (" " + " ".join(parts)) if parts else ""


class _TopBlocks(HTMLParser):
    """Split HTML into top-level block element serializations (inside article or whole frag)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self.prefix: list[str] = []
        self.suffix: list[str] = []
        self._depth = 0
        self._buf: list[str] = []
        self._in_article = False
        self._after_article = False
        self._saw_article = False

    def _collecting_blocks(self) -> bool:
        if self._after_article:
            return False
        # With <article>: only inside it. Without: whole document is blocks.
        return self._in_article or not self._saw_article

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        raw = f"<{tag}{_attr_str(attrs)}>"
        if tag_l == "article" and self._depth == 0 and not self._in_article:
            self._saw_article = True
            self._in_article = True
            self.prefix.append(raw)
            return
        if tag_l in _VOID:
            if self._depth == 0:
                if self._collecting_blocks():
                    self.blocks.append(raw)
                elif not self._in_article:
                    self.prefix.append(raw)
                else:
                    self.suffix.append(raw)
            else:
                self._buf.append(raw)
            return
        if tag_l in _BLOCK_TAGS and self._depth == 0 and self._collecting_blocks():
            self._depth = 1
            self._buf = [raw]
            return
        if self._depth:
            self._depth += 1
            self._buf.append(raw)
            return
        if not self._in_article and not self._saw_article:
            self.prefix.append(raw)
        elif self._after_article:
            self.suffix.append(raw)

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        raw = f"</{tag}>"
        if tag_l == "article" and self._in_article and self._depth == 0 and not self._after_article:
            self.suffix.insert(0, raw)
            self._after_article = True
            return
        if self._depth:
            self._buf.append(raw)
            self._depth -= 1
            if self._depth == 0:
                self.blocks.append("".join(self._buf))
                self._buf = []
            return
        if not self._in_article:
            self.prefix.append(raw)
        else:
            self.suffix.append(raw)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._buf.append(data)
        elif not self._in_article:
            self.prefix.append(data)
        elif self._after_article:
            self.suffix.append(data)
        elif data.strip():
            # Loose text inside article — keep as block-ish chunk
            self.blocks.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")


def split_article_blocks(html: str) -> tuple[str, list[str], str]:
    """Return (prefix_including_article_open, blocks, suffix_including_article_close)."""
    p = _TopBlocks()
    p.feed(html or "")
    p.close()
    if p._in_article or p.blocks:
        return "".join(p.prefix), p.blocks, "".join(p.suffix)
    # No article wrapper — treat whole as blocks
    return "", p.blocks or ([html] if (html or "").strip() else []), ""


class _TextOf(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def visible_text(html: str) -> str:
    p = _TextOf()
    p.feed(html or "")
    p.close()
    return _WS.sub(" ", "".join(p.parts)).strip()


def block_classes(html: str) -> set[str]:
    m = re.search(r"<([a-zA-Z0-9]+)([^>]*)>", html or "")
    if not m:
        return set()
    attrs = m.group(2)
    cm = re.search(r'\bclass=["\']([^"\']+)["\']', attrs, re.I)
    if not cm:
        return set()
    return set(cm.group(1).lower().split())


def is_iast_block(html: str) -> bool:
    return "iast" in block_classes(html)


def is_sa_block(html: str) -> bool:
    cls = block_classes(html)
    if "iast" in cls and "sa" not in cls:
        return False
    if "ru" in cls and "sa" not in cls:
        return False
    if "sa" in cls or "shloka" in cls:
        return True
    # Devanagari-heavy block without ru/iast class
    text = visible_text(html)
    if not text:
        return False
    deva = sum(1 for ch in text if "\u0900" <= ch <= "\u097F")
    return deva >= max(4, len(text) // 4) and "ru" not in cls and "iast" not in cls


def extract_sa_iast_pairs(iast_html: str) -> list[tuple[str, str]]:
    """Ordered (normalized_sa, iast_element_html) from an iast_block page."""
    _, blocks, _ = split_article_blocks(iast_html)
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if is_sa_block(b) and nxt and is_iast_block(nxt):
            pairs.append((normalize_sa(visible_text(b)), nxt.strip()))
            i += 2
            continue
        if is_iast_block(b) and not is_sa_block(b):
            # orphan iast — keep with empty sa key so sequential fill can still use it
            pairs.append(("", b.strip()))
        i += 1
    return pairs


def _score(sa_norm: str, key: str) -> float:
    if not sa_norm and not key:
        return 0.0
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
    """Return (iast_html, new_cursor). Prefer near-cursor then best unused match."""
    best_i = -1
    best_s = 0.0
    # Prefer sequential: look at cursor .. cursor+6 first
    window = list(range(cursor, min(len(pairs), cursor + 8)))
    window += [i for i in range(len(pairs)) if i not in window]
    for i in window:
        if i in used:
            continue
        key, html = pairs[i]
        sc = _score(sa_norm, key) if key else (0.55 if not sa_norm else 0.0)
        # sequential bonus
        if i == cursor:
            sc += 0.08
        elif abs(i - cursor) <= 2:
            sc += 0.03
        if sc > best_s:
            best_s = sc
            best_i = i
    if best_i < 0 or best_s < 0.62:
        return None, cursor
    used.add(best_i)
    return pairs[best_i][1], best_i + 1


def merge_iast_into_translation(ru_html: str, iast_html: str) -> tuple[str, dict[str, int]]:
    """Insert IAST blocks after each Sanskrit block in the Russian draft.

    Skips sa blocks that already have an IAST sibling. Returns (html, stats).
    """
    pairs = extract_sa_iast_pairs(iast_html)
    prefix, blocks, suffix = split_article_blocks(ru_html)
    if not blocks and not pairs:
        return ru_html or "", {"inserted": 0, "skipped_existing": 0, "unmatched_sa": 0, "pairs": 0}

    used: set[int] = set()
    cursor = 0
    out: list[str] = []
    inserted = 0
    skipped = 0
    unmatched = 0

    i = 0
    while i < len(blocks):
        b = blocks[i]
        out.append(b)
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if is_sa_block(b):
            if nxt and is_iast_block(nxt):
                skipped += 1
                i += 1
                continue
            sa_norm = normalize_sa(visible_text(b))
            iast_el, cursor = pick_iast(sa_norm, pairs, used, cursor=cursor)
            if iast_el:
                out.append(iast_el)
                inserted += 1
            else:
                unmatched += 1
        i += 1

    if not prefix and "<article" not in (ru_html or "").lower():
        body = "\n".join(out)
        html = f'<article class="page-style" lang="ru">\n{body}\n</article>\n'
    else:
        html = prefix + "\n".join(out) + suffix
        if prefix and not html.endswith("\n"):
            html += "\n"

    return html, {
        "inserted": inserted,
        "skipped_existing": skipped,
        "unmatched_sa": unmatched,
        "pairs": len(pairs),
        "used_pairs": len(used),
    }
