"""Build a downloadable PDF from project page HTML (PyMuPDF Story).

Modes: text (HTML only) | interleave (scan page then HTML for each source page).

Scans are embedded via insert_image (Story file:// imgs break). Devanagari fonts
come from an Archive over the system Noto tree. No page chrome / tripundra overlays.
"""
from __future__ import annotations

import re
import tempfile
import uuid
from pathlib import Path

import fitz

from app.services.storage import ensure_dirs

# Compact book measure: dense Devanagari scans need ~8pt on a page
# slightly taller than A5 so one source page → one PDF page.
CSS = """
body {
  font-family: "Noto Serif Devanagari", "Noto Sans Devanagari", "FreeSerif", serif;
  font-size: 8pt;
  /* ≥1.4 so anudātta (॒) below does not collide with the next line */
  line-height: 1.42;
  color: #1a1814;
  background-color: #f7f2e8;
}
p { margin: 0.18em 0; }
.cover { text-align: center; margin: 1em 0 0.3em; }
.cover-title { font-size: 13pt; font-weight: 700; color: #1a1814; margin: 0.4em 0 0.15em; line-height: 1.25; }
.cover-title-sa {
  font-family: "Noto Serif Devanagari", "Noto Sans Devanagari", serif;
  font-size: 10.5pt; color: #5c3d2e; margin: 0.1em 0 0.45em;
}
.cover-brand { font-size: 7.5pt; color: #6b6560; margin-top: 1em; }
.page, .page-style { margin: 0 auto; max-width: 96%; }
.narrow { max-width: 84%; margin-left: auto; margin-right: auto; }
.type-sm { font-size: 7pt; }
.type-md { font-size: 8pt; }
.type-lg { font-size: 9pt; }
.lh-tight { line-height: 1.28; }
.lh-normal { line-height: 1.42; }
.lh-loose { line-height: 1.62; }
.compact p { margin: 0.1em 0; }
.indent { text-indent: 1.2em; }
h1 {
  text-align: center; font-size: 10.5pt; margin: 0.1em 0 0.35em; color: #3d2914; line-height: 1.3;
}
h1.sa, .sa, span.sa, p.sa, footer.sa {
  font-family: "Noto Serif Devanagari", "Noto Sans Devanagari", serif;
}
.shloka, p.shloka {
  text-align: center; margin: 0.28em auto; max-width: 88%; line-height: inherit; color: #1a1814;
}
.running-head { text-align: center; font-size: 7.5pt; color: #6b6560; margin: 0 0 0.35em; }
.centered { text-align: center; }
.page-num { text-align: center; font-size: 7.5pt; color: #6b6560; margin-top: 0.5em; }
.page-break { page-break-before: always; }
footer, .footer {
  text-align: center; margin-top: 0.5em; font-size: 7.5pt; color: #6b6560;
}
img { max-width: 88%; max-height: 280pt; }
figure.page-figure { text-align: center; margin: 0.3em 0; }
figure.page-figure img { max-width: 85%; max-height: 260pt; }
table { width: 100%; border-collapse: collapse; margin: 0.3em 0; font-size: inherit; }
td, th { border: 0.5pt solid #c4b8a8; padding: 0.08em 0.2em; }
th { background: #efe6d8; color: #3d2914; }
"""

# Fallback when no scan aspect is available (Indian crown-ish; taller than A5).
_PAGE_W = 412.0
_PAGE_H = 612.0
_MARGIN = 14  # Story content inset on each side

_API_IMG_RE = re.compile(
    r'src=["\']/api/v1/pages/([0-9a-fA-F-]{36})/figures/([^"\']+)["\']',
    re.I,
)

_FONT_DIRS = (
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/noto",
)

# Cap scan plate long side so 600+ page interleave fits small VPS RAM/disk.
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
    """pages: list of (page_no, html_fragment, scan_path|None).

    mode=text — HTML only (default).
    mode=interleave — for each page: full scan plate, then HTML (скан ‖ текст).
    """
    mode = (mode or "text").strip().lower()
    if mode not in ("text", "interleave"):
        mode = "text"

    out_dir = ensure_dirs() / "exports" / str(project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-interleave" if mode == "interleave" else ""
    out_path = out_dir / f"{slug}{suffix}.pdf"

    mediabox = _mediabox_for_pages(pages)
    doc = fitz.open()
    body_n = 0

    cover_doc = _html_to_doc(_cover_html(title, title_sa), mediabox)
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
            frag, arch_extra = _rewrite_img_srcs_for_pdf(frag, project_id, page_no)
            text_html = f"<section class='page' data-page='{page_no}'>{frag}</section>"
            text_doc = _html_to_doc(text_html, mediabox, extra_archive=arch_extra)
            doc.insert_pdf(text_doc)
            text_doc.close()
            body_n += 1

    if body_n == 0:
        empty = _html_to_doc("<p class='centered'>(нет страниц с текстом для выгрузки)</p>", mediabox)
        doc.insert_pdf(empty)
        empty.close()

    tmp_out = out_path.with_suffix(f".{uuid.uuid4().hex}.tmp.pdf")
    doc.save(tmp_out.as_posix(), garbage=3, deflate=True)
    doc.close()
    tmp_out.replace(out_path)
    return out_path


def _mediabox_for_pages(pages: list[tuple[int, str, str | None]]) -> fitz.Rect:
    """Pick page size from the first scan's aspect (width≈source book).

    A5 (420×595) is shorter than many Indian scans (~411×612), which forced
    dense pages to spill onto a second PDF sheet.
    """
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


def _font_archive(extra: fitz.Archive | None = None) -> fitz.Archive:
    roots = [d for d in _FONT_DIRS if Path(d).is_dir()]
    arch = fitz.Archive(*roots) if roots else fitz.Archive()
    if extra is not None:
        arch.add(extra)
    return arch


def _html_to_doc(
    body_html: str,
    mediabox: fitz.Rect,
    *,
    extra_archive: fitz.Archive | None = None,
) -> fitz.Document:
    """Render a body fragment to an in-memory PDF Document via Story."""
    doc_html = (
        "<html><head><meta charset='utf-8'></head>"
        f"<body>{body_html}</body></html>"
    )
    m = _MARGIN
    where = mediabox + (m, m, -m, -m)
    story = fitz.Story(html=doc_html, user_css=CSS, archive=_font_archive(extra_archive))
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        writer = fitz.DocumentWriter(tmp_path.as_posix())
        more = True
        while more:
            device = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(device)
            writer.end_page()
        writer.close()
        doc = fitz.open(tmp_path.as_posix())
        mem = fitz.open()
        mem.insert_pdf(doc)
        doc.close()
        return mem
    finally:
        tmp_path.unlink(missing_ok=True)


def _append_scan_page(doc: fitz.Document, mediabox: fitz.Rect, scan_path: Path) -> None:
    """Append one page with the scan image (JPEG-compressed, downscaled)."""
    page = doc.new_page(width=mediabox.width, height=mediabox.height)
    rect = page.rect  # full-bleed — no chrome margins
    stream = _scan_jpeg_bytes(scan_path)
    if stream:
        page.insert_image(rect, stream=stream, keep_proportion=True)
    else:
        page.insert_image(rect, filename=scan_path.as_posix(), keep_proportion=True)


def _scan_jpeg_bytes(scan_path: Path) -> bytes | None:
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(scan_path) as img:
            img = img.convert("RGB")
            img.thumbnail((_SCAN_MAX_PX, _SCAN_MAX_PX), Image.Resampling.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=_SCAN_JPEG_Q, optimize=True)
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _rewrite_img_srcs_for_pdf(
    html: str, project_id: uuid.UUID, page_no: int
) -> tuple[str, fitz.Archive | None]:
    """Map /api/.../figures/name to archive aliases; return (html, archive|None)."""
    from app.services.layout_assets import figure_file

    arch = fitz.Archive()
    added = 0

    def repl(m: re.Match) -> str:
        nonlocal added
        name = m.group(2)
        path = figure_file(project_id, page_no, name)
        if path is None or not path.is_file():
            return m.group(0)
        alias = f"fig-{page_no:04d}-{Path(name).name}"
        try:
            arch.add(path.as_posix(), alias)
            added += 1
        except Exception:  # noqa: BLE001
            return m.group(0)
        return f'src="{alias}"'

    new_html = _API_IMG_RE.sub(repl, html)
    return new_html, (arch if added else None)


def _cover_html(title: str, title_sa: str | None) -> str:
    sa = (title_sa or "").strip()
    sa_block = f"<p class='cover-title-sa sa'>{_esc(sa)}</p>" if sa else ""
    return f"""
<section class="cover">
  <h1 class="cover-title">{_esc(title)}</h1>
  {sa_block}
  <p class="cover-brand">Sanskrit SRV</p>
</section>
"""


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
