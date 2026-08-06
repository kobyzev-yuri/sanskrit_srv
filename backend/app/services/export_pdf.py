"""Build a downloadable PDF from project page HTML.

Modes: text (HTML only) | interleave (scan page then HTML for each source page).

Text is drawn with fitz.Font(fontfile=…) + TextWriter so Devanagari keeps a
correct ToUnicode map (PyMuPDF Story Identity-H breaks copy/paste into Word).
"""
from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import fitz

from app.services.storage import ensure_dirs

# Compact book measure: dense Devanagari scans need ~7.5pt on a page
# slightly taller than A5 so one source page → one PDF page.
_PAGE_W = 412.0
_PAGE_H = 612.0
_MARGIN = 12

_BODY_PT = 7.5
_H1_PT = 10.5
_SMALL_PT = 7.0
_LINE_GAP = 1.35
_BG = (247 / 255, 242 / 255, 232 / 255)
_INK = (0x1A / 255, 0x18 / 255, 0x14 / 255)
_MUTED = (0x6B / 255, 0x65 / 255, 0x60 / 255)

_API_IMG_RE = re.compile(
    r"/api/v1/pages/([0-9a-fA-F-]{36})/figures/([^\"'\s>]+)",
    re.I,
)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSerifDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
)

_SCAN_MAX_PX = 1400
_SCAN_JPEG_Q = 78


def build_project_pdf(
    project_id: uuid.UUID,
    slug: str,
    title: str,
    pages: list[tuple[int, str, str | None]],
    *,
    title_sa: str | None = None,
    mode: str = "text",
) -> Path:
    """pages: list of (page_no, html_fragment, scan_path|None)."""
    mode = (mode or "text").strip().lower()
    if mode not in ("text", "interleave"):
        mode = "text"

    out_dir = ensure_dirs() / "exports" / str(project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-interleave" if mode == "interleave" else ""
    out_path = out_dir / f"{slug}{suffix}.pdf"

    mediabox = _mediabox_for_pages(pages)
    font = _deva_font()
    doc = fitz.open()
    body_n = 0

    cover_doc = _blocks_to_doc(
        [
            _Block(text=title or "Untitled", size=12.0, align=1, color=_INK, gap_after=6),
            *(
                [_Block(text=title_sa.strip(), size=10.0, align=1, color=_MUTED, gap_after=8)]
                if (title_sa or "").strip()
                else []
            ),
            _Block(text="Sanskrit SRV", size=_SMALL_PT, align=1, color=_MUTED, gap_after=0),
        ],
        mediabox,
        font,
    )
    doc.insert_pdf(cover_doc)
    cover_doc.close()

    for page_no, html, scan_path in pages:
        frag = (html or "").strip()
        if not frag and mode == "text":
            continue

        if mode == "interleave" and scan_path and Path(scan_path).is_file():
            _append_scan_page(doc, mediabox, Path(scan_path))
            body_n += 1

        if frag:
            blocks = _html_to_blocks(frag, project_id, page_no)
            text_doc = _blocks_to_doc(blocks, mediabox, font)
            doc.insert_pdf(text_doc)
            text_doc.close()
            body_n += 1

    if body_n == 0:
        empty = _blocks_to_doc(
            [_Block(text="(нет страниц с текстом для выгрузки)", size=_BODY_PT, align=1)],
            mediabox,
            font,
        )
        doc.insert_pdf(empty)
        empty.close()

    # Word paste ignores/misreads compact bfrange ToUnicode on Identity-H Indic.
    _fix_tounicode_for_word(doc)

    tmp_out = out_path.with_suffix(f".{uuid.uuid4().hex}.tmp.pdf")
    doc.save(tmp_out.as_posix(), garbage=3, deflate=True)
    doc.close()
    tmp_out.replace(out_path)
    return out_path


def _fix_tounicode_for_word(doc: fitz.Document) -> None:
    """Rewrite each embedded font's ToUnicode as explicit bfchar from the subset cmap.

    PyMuPDF's default maps use large beginbfrange runs. Acrobat display and MuPDF
    extract stay fine, but Word copy/paste often decodes Devanagari CIDs as Latin
    junk. Chromium-style bfchar maps paste correctly into Word.
    """
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return

    from io import BytesIO

    seen: set[int] = set()
    for page in doc:
        for item in page.get_fonts(full=True):
            xref = item[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                extracted = doc.extract_font(xref)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(extracted, dict):
                buf = extracted.get("content") or b""
            elif isinstance(extracted, (tuple, list)) and len(extracted) >= 4:
                buf = extracted[3] or b""
            else:
                continue
            if not buf or len(buf) < 100:
                continue
            try:
                tt = TTFont(BytesIO(buf))
                best = tt.getBestCmap() or {}
                order = tt.getGlyphOrder()
            except Exception:  # noqa: BLE001
                continue
            name_to_gid = {n: i for i, n in enumerate(order)}
            mapping: dict[int, str] = {}
            for uni, gname in best.items():
                gid = name_to_gid.get(gname)
                if gid is None or uni <= 0:
                    continue
                mapping.setdefault(gid, chr(uni))
            if not mapping:
                continue
            font_obj = doc.xref_object(xref)
            m = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", font_obj)
            if not m:
                continue
            tu_xref = int(m.group(1))
            try:
                doc.update_stream(tu_xref, _tounicode_bfchar_stream(mapping))
            except Exception:  # noqa: BLE001
                continue


def _tounicode_bfchar_stream(mapping: dict[int, str]) -> bytes:
    items = sorted((cid, text) for cid, text in mapping.items() if text)
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    # PDF allows at most 100 entries per beginbfchar block.
    chunk = 100
    for i in range(0, len(items), chunk):
        part = items[i : i + chunk]
        lines.append(f"{len(part)} beginbfchar")
        for cid, text in part:
            dst = text.encode("utf-16-be").hex().upper()
            lines.append(f"<{cid:04X}> <{dst}>")
        lines.append("endbfchar")
    lines.extend(
        [
            "endcmap",
            "CMapName currentdict /CMap defineresource pop",
            "end",
            "end",
        ]
    )
    return ("\n".join(lines) + "\n").encode("ascii")


def _mediabox_for_pages(pages: list[tuple[int, str, str | None]]) -> fitz.Rect:
    for _, _, scan_path in pages:
        if not scan_path:
            continue
        path = Path(scan_path)
        if not path.is_file():
            continue
        try:
            from PIL import Image

            with Image.open(path) as img:
                w, h = img.size
            if w > 0 and h > 0:
                width = _PAGE_W
                height = width * (h / w)
                height = max(560.0, min(height, 700.0))
                return fitz.Rect(0, 0, width, height)
        except Exception:  # noqa: BLE001
            continue
    return fitz.Rect(0, 0, _PAGE_W, _PAGE_H)


def _deva_font() -> fitz.Font:
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            return fitz.Font(fontfile=path)
    return fitz.Font("helv")


@dataclass
class _Block:
    text: str = ""
    size: float = _BODY_PT
    align: int = 0  # 0 left, 1 center, 2 right
    color: tuple[float, float, float] = _INK
    gap_after: float = 3.0
    indent: float = 0.0
    image: Path | None = None
    table: list[list[str]] = field(default_factory=list)


def _blocks_to_doc(
    blocks: list[_Block], mediabox: fitz.Rect, font: fitz.Font
) -> fitz.Document:
    """Paint blocks with TextWriter — correct Unicode copy/paste."""
    doc = fitz.open()
    m = _MARGIN
    content = fitz.Rect(m, m, mediabox.width - m, mediabox.height - m)
    page = doc.new_page(width=mediabox.width, height=mediabox.height)
    page.draw_rect(page.rect, color=None, fill=_BG)
    y = content.y0
    max_w = content.width

    def new_page() -> fitz.Page:
        nonlocal page, y
        page = doc.new_page(width=mediabox.width, height=mediabox.height)
        page.draw_rect(page.rect, color=None, fill=_BG)
        y = content.y0
        return page

    for block in blocks:
        if block.image and block.image.is_file():
            img_h = min(280.0, content.y1 - y - 4)
            if img_h < 40:
                new_page()
                img_h = min(280.0, content.height - 4)
            img_w = max_w * 0.88
            rect = fitz.Rect(
                content.x0 + (max_w - img_w) / 2,
                y,
                content.x0 + (max_w - img_w) / 2 + img_w,
                y + img_h,
            )
            try:
                page.insert_image(rect, filename=block.image.as_posix(), keep_proportion=True)
            except Exception:  # noqa: BLE001
                pass
            y = rect.y1 + block.gap_after
            continue

        if block.table:
            page, y = _draw_table(
                page, font, block.table, content.x0, y, max_w, content.y1, new_page
            )
            y += block.gap_after
            continue

        text = (block.text or "").strip()
        if not text:
            y += block.gap_after * 0.5
            continue

        size = block.size
        leading = size * _LINE_GAP
        lines = _wrap_lines(font, text, max_w - block.indent, size)
        for line in lines:
            if y + leading > content.y1:
                new_page()
            tw = fitz.TextWriter(page.rect, color=block.color)
            w = font.text_length(line, fontsize=size)
            if block.align == 1:
                x = content.x0 + max((max_w - w) / 2, 0)
            elif block.align == 2:
                x = content.x1 - w
            else:
                x = content.x0 + block.indent
            tw.append((x, y + size * 0.85), line, font=font, fontsize=size)
            tw.write_text(page)
            y += leading
        y += block.gap_after

    return doc


def _wrap_lines(font: fitz.Font, text: str, max_w: float, size: float) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        trial = f"{cur} {w}"
        if font.text_length(trial, fontsize=size) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _draw_table(
    page: fitz.Page,
    font: fitz.Font,
    rows: list[list[str]],
    x0: float,
    y: float,
    max_w: float,
    y1: float,
    new_page,
) -> tuple[fitz.Page, float]:
    if not rows:
        return page, y
    cols = max(len(r) for r in rows)
    size = _SMALL_PT
    leading = size * 1.3
    col_w = max_w / cols
    for row in rows:
        need = leading + 4
        if y + need > y1:
            page = new_page()
            y = _MARGIN
        for ci in range(cols):
            cell = row[ci] if ci < len(row) else ""
            cx = x0 + ci * col_w
            rect = fitz.Rect(cx, y, cx + col_w, y + need)
            page.draw_rect(rect, color=(0.77, 0.72, 0.66), width=0.4)
            tw = fitz.TextWriter(page.rect, color=_INK)
            shown = cell
            while shown and font.text_length(shown, fontsize=size) > col_w - 4:
                shown = shown[:-1]
            tw.append((cx + 2, y + size * 0.9), shown, font=font, fontsize=size)
            tw.write_text(page)
        y += need
    return page, y


def _html_to_blocks(
    html: str, project_id: uuid.UUID, page_no: int
) -> list[_Block]:
    parser = _HtmlToBlocks(project_id, page_no)
    parser.feed(html)
    parser.close()
    parser.flush()
    return parser.blocks


class _HtmlToBlocks(HTMLParser):
    _BLOCK = frozenset(
        {"p", "h1", "h2", "h3", "footer", "li", "div", "section", "article", "figcaption"}
    )

    def __init__(self, project_id: uuid.UUID, page_no: int):
        super().__init__(convert_charrefs=True)
        self.project_id = project_id
        self.page_no = page_no
        self.blocks: list[_Block] = []
        self._buf: list[str] = []
        self._size = _BODY_PT
        self._align = 0
        self._color = _INK
        self._indent = 0.0
        self._skip = 0
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._in_td = False
        self._td_buf: list[str] = []
        self._classes: list[str] = []

    def flush(self) -> None:
        self._emit_text()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        ad = {k.lower(): (v or "") for k, v in attrs}
        classes = (ad.get("class") or "").split()

        if tag in ("script", "style"):
            self._skip += 1
            return
        if self._skip:
            return

        if tag == "br":
            self._buf.append("\n")
            return

        if tag == "img":
            self._emit_text()
            path = _resolve_figure_path(ad.get("src", ""), self.project_id, self.page_no)
            if path:
                self.blocks.append(_Block(image=path, gap_after=6))
            return

        if tag == "table":
            self._emit_text()
            self._table = []
            return
        if tag == "tr" and self._table is not None:
            self._row = []
            self._table.append(self._row)
            return
        if tag in ("td", "th") and self._row is not None:
            self._in_td = True
            self._td_buf = []
            return

        if tag in self._BLOCK:
            self._emit_text()
            self._classes = classes
            self._size = _BODY_PT
            self._align = 0
            self._color = _INK
            self._indent = 0.0
            if tag == "h1" or "type-lg" in classes or "cover-title" in classes:
                self._size = _H1_PT
            elif "type-sm" in classes or "running-head" in classes or "page-num" in classes or "cover-brand" in classes:
                self._size = _SMALL_PT
                self._color = _MUTED
            elif "type-md" in classes:
                self._size = _BODY_PT
            if any(
                c in classes
                for c in ("centered", "shloka", "running-head", "page-num", "cover", "cover-title", "cover-title-sa", "cover-brand")
            ) or tag in ("h1", "footer"):
                self._align = 1
            if "indent" in classes:
                self._indent = 12.0
            return

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("td", "th") and self._in_td and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._td_buf)).strip())
            self._in_td = False
            return
        if tag == "table" and self._table is not None:
            rows = [r for r in self._table if any(c.strip() for c in r)]
            if rows:
                self.blocks.append(_Block(table=rows, gap_after=6))
            self._table = None
            self._row = None
            return
        if tag in self._BLOCK:
            self._emit_text()

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        if self._in_td:
            self._td_buf.append(data)
            return
        self._buf.append(data)

    def _emit_text(self) -> None:
        raw = "".join(self._buf)
        self._buf = []
        # HTML whitespace; keep deliberate <br> as paragraph breaks
        parts = raw.split("\n")
        for i, part in enumerate(parts):
            text = re.sub(r"[ \t\r\f\v]+", " ", part).strip()
            if not text and i < len(parts) - 1:
                continue
            if not text:
                continue
            self.blocks.append(
                _Block(
                    text=text,
                    size=self._size,
                    align=self._align,
                    color=self._color,
                    indent=self._indent,
                    gap_after=3.0,
                )
            )


def _resolve_figure_path(
    src: str, project_id: uuid.UUID, page_no: int
) -> Path | None:
    from app.services.layout_assets import figure_file

    if not src:
        return None
    # Story aliases fig-NNNN-name.png or API URLs
    m = _API_IMG_RE.search(src)
    name = m.group(2) if m else Path(src.split("?")[0]).name
    name = re.sub(r"^fig-\d{4}-", "", name)
    if re.fullmatch(r"(emb|crop)-\d{2}\.png", name or ""):
        return figure_file(project_id, page_no, name)
    return None


def _append_scan_page(doc: fitz.Document, mediabox: fitz.Rect, scan_path: Path) -> None:
    page = doc.new_page(width=mediabox.width, height=mediabox.height)
    rect = page.rect
    stream = _scan_jpeg_bytes(scan_path)
    if stream:
        page.insert_image(rect, stream=stream, keep_proportion=True)
    else:
        page.insert_image(rect, filename=scan_path.as_posix(), keep_proportion=True)


def _scan_jpeg_bytes(scan_path: Path) -> bytes | None:
    try:
        from PIL import Image

        with Image.open(scan_path) as img:
            img = img.convert("RGB")
            img.thumbnail((_SCAN_MAX_PX, _SCAN_MAX_PX), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=_SCAN_JPEG_Q, optimize=True)
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None
