"""Split page HTML into sized chunks for LLM translate; merge translated parts."""
from __future__ import annotations

import re

# Soft target / hard cap for SOURCE HTML per LLM call. Single huge block may exceed hard.
CHUNK_SOFT = 6000
CHUNK_HARD = 9000
# Below this, send the whole page in one request.
MIN_SPLIT = 8000

_ARTICLE_OPEN_RE = re.compile(r"(?is)^\s*(<article\b[^>]*>)\s*")
_ARTICLE_CLOSE_RE = re.compile(r"(?is)\s*(</article>)\s*$")
_TAG_RE = re.compile(r"<!--.*?-->|</?([a-zA-Z][\w:-]*)\b[^>]*>", re.S)
_VOID = frozenset({"hr", "br", "img", "meta", "link", "input", "wbr", "area", "base", "col", "embed", "source", "track"})


def unwrap_article(html: str) -> tuple[str, str, str]:
    """Return (open_tag_or_empty, inner, close_tag_or_empty)."""
    text = html or ""
    open_m = _ARTICLE_OPEN_RE.search(text)
    close_m = _ARTICLE_CLOSE_RE.search(text)
    if open_m and close_m and open_m.end() <= close_m.start():
        return open_m.group(1), text[open_m.end() : close_m.start()], close_m.group(1)
    if open_m:
        return open_m.group(1), text[open_m.end() :], ""
    return "", text, ""


def extract_article_inner(html: str) -> str:
    _, inner, _ = unwrap_article(html)
    return inner.strip() if inner.strip() else (html or "").strip()


def split_top_level_blocks(inner: str) -> list[str]:
    """Split HTML into top-level sibling blocks (tags + loose text)."""
    if not (inner or "").strip():
        return []
    blocks: list[str] = []
    depth = 0
    start = 0
    for m in _TAG_RE.finditer(inner):
        tag = m.group(0)
        name = (m.group(1) or "").lower()
        is_comment = tag.startswith("<!--")
        is_close = tag.startswith("</")
        self_closing = is_comment or tag.endswith("/>") or name in _VOID

        if depth == 0 and not is_close:
            if m.start() > start:
                text = inner[start : m.start()]
                if text.strip():
                    blocks.append(text)
                elif text and blocks:
                    blocks[-1] += text
            start = m.start()

        if is_comment:
            if depth == 0:
                blocks.append(inner[start : m.end()])
                start = m.end()
            continue

        if is_close:
            depth = max(0, depth - 1)
            if depth == 0:
                blocks.append(inner[start : m.end()])
                start = m.end()
        elif self_closing:
            if depth == 0:
                blocks.append(inner[start : m.end()])
                start = m.end()
        else:
            depth += 1

    if start < len(inner):
        tail = inner[start:]
        if tail.strip():
            blocks.append(tail)
        elif tail and blocks:
            blocks[-1] += tail
    return blocks or ([inner] if inner.strip() else [])


def pack_blocks(blocks: list[str], *, soft: int = CHUNK_SOFT, hard: int = CHUNK_HARD) -> list[str]:
    """Greedy-pack blocks into chunks under soft/hard size limits."""
    if not blocks:
        return []
    chunks: list[str] = []
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            chunks.append("".join(buf))
            buf = []
            size = 0

    for block in blocks:
        blen = len(block)
        if buf and size + blen > soft and size > 0:
            flush()
        if buf and size + blen > hard:
            flush()
        buf.append(block)
        size += blen
        if size >= hard:
            flush()
    flush()
    return chunks


def chunk_page_html(
    html: str,
    *,
    soft: int = CHUNK_SOFT,
    hard: int = CHUNK_HARD,
    min_split: int = MIN_SPLIT,
) -> list[str]:
    """Return SOURCE fragments to translate (each wrapped like the original article).

    Small pages → one fragment. Large pages → several top-level-block packs.
    """
    text = (html or "").strip()
    if not text:
        return []
    open_tag, inner, close_tag = unwrap_article(text)
    body = inner if open_tag else text
    if len(body) < min_split:
        return [text]

    blocks = split_top_level_blocks(body)
    packed = pack_blocks(blocks, soft=soft, hard=hard)
    if len(packed) <= 1:
        return [text]

    open_tag = open_tag or '<article class="page-style">'
    close_tag = close_tag or "</article>"
    return [f"{open_tag}\n{chunk.strip()}\n{close_tag}" for chunk in packed]


def merge_translated_chunks(parts: list[str], *, article_open: str | None = None) -> str:
    """Merge translated HTML parts into one <article>…</article>."""
    if not parts:
        return ""
    if len(parts) == 1:
        only = parts[0].strip()
        if "<article" in only.lower():
            return only
        open_tag = (article_open or '<article class="page-style" lang="ru">').strip()
        return f"{open_tag}\n{only}\n</article>"

    inners = [extract_article_inner(p) for p in parts if (p or "").strip()]
    open_tag = (article_open or "").strip()
    if not open_tag:
        first_open, _, _ = unwrap_article(parts[0])
        open_tag = first_open or '<article class="page-style" lang="ru">'
    # Prefer lang=ru on the outer wrapper for translation output.
    if 'lang="' not in open_tag.lower():
        open_tag = open_tag[:-1] + ' lang="ru">' if open_tag.endswith(">") else open_tag
    elif 'lang="sa"' in open_tag.lower():
        open_tag = re.sub(r'lang=["\']sa["\']', 'lang="ru"', open_tag, count=1, flags=re.I)
    body = "\n".join(inners)
    return f"{open_tag}\n{body}\n</article>"
