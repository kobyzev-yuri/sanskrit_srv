"""Split translation HTML into Sanskrit / Russian block pairs for spreadsheets."""
from __future__ import annotations

from html.parser import HTMLParser


_BLOCK = frozenset({"p", "h1", "h2", "h3", "li", "footer"})


class _BlockParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self._kind: str | None = None
        self._buf: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._depth += 1
            return
        if tag not in _BLOCK or self._kind is not None:
            return
        classes = []
        for k, v in attrs:
            if k.lower() == "class" and v:
                classes = v.lower().split()
                break
        if "ru" in classes:
            self._kind = "ru"
        elif "sa" in classes or "shloka" in classes:
            self._kind = "sa"
        else:
            self._kind = "other"
        self._buf = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style") and self._depth:
            self._depth -= 1
            return
        if tag not in _BLOCK or self._kind is None:
            return
        text = " ".join("".join(self._buf).split()).strip()
        if text:
            self.blocks.append((self._kind, text))
        self._kind = None
        self._buf = []

    def handle_data(self, data: str) -> None:
        if self._kind is not None and self._depth == 0:
            self._buf.append(data)


def html_blocks(html: str) -> list[tuple[str, str]]:
    parser = _BlockParser()
    parser.feed(html or "")
    parser.close()
    return parser.blocks


def pair_sa_ru(blocks: list[tuple[str, str]], page_no: int) -> list[tuple[int, str, str]]:
    """(page_no, sanskrit, russian) — pair a sa block with the following ru block."""
    rows: list[tuple[int, str, str]] = []
    i = 0
    while i < len(blocks):
        kind, text = blocks[i]
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if kind == "sa" and nxt and nxt[0] == "ru":
            rows.append((page_no, text, nxt[1]))
            i += 2
            continue
        if kind == "ru":
            rows.append((page_no, "", text))
        else:
            rows.append((page_no, text, ""))
        i += 1
    return rows


def translation_rows(pages: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    """pages: (page_no, translation_html)."""
    out: list[tuple[int, str, str]] = []
    for page_no, html in pages:
        frag = (html or "").strip()
        if not frag:
            continue
        out.extend(pair_sa_ru(html_blocks(frag), page_no))
    return out
