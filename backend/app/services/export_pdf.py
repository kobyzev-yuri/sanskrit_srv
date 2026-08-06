"""Build a downloadable PDF from project page HTML.

Modes: text (HTML only) | interleave (scan page then HTML for each source page).

Text pages are rendered with headless Chromium when available (correct Devanagari
shaping). Chromium/poppler then insert spurious U+0020 between glyph clusters on
copy (reordered ि, conjuncts like कृष्…). We convert painted text to paths and
overlay MuPDF's clean Unicode as invisible text so Word/Chrome copy stays intact.
Falls back to PyMuPDF Story if Chromium is missing.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import fitz

from app.services.storage import ensure_dirs

log = logging.getLogger("sanskrit.export_pdf")

# Compact book measure: dense Devanagari scans need ~7.5pt on a page
# slightly taller than A5 so one source page → one PDF page.
CSS = """
body {
  font-family: "Noto Serif Devanagari", "Noto Sans Devanagari", "FreeSerif", serif;
  font-size: 7.5pt;
  line-height: 1.35;
  color: #1a1814;
  background-color: #f7f2e8;
  font-synthesis: none;
  text-rendering: optimizeLegibility;
  font-feature-settings: "liga" 1, "clig" 1, "calt" 1, "locl" 1;
  letter-spacing: 0;
  word-spacing: normal;
}
p { margin: 0.14em 0; }
.cover { text-align: center; margin: 1em 0 0.3em; }
.cover-title { font-size: 12pt; font-weight: 700; color: #1a1814; margin: 0.4em 0 0.15em; line-height: 1.25; }
.cover-title-sa {
  font-family: "Noto Serif Devanagari", "Noto Sans Devanagari", serif;
  font-size: 10pt; color: #5c3d2e; margin: 0.1em 0 0.45em;
}
.cover-brand { font-size: 7pt; color: #6b6560; margin-top: 1em; }
.page, .page-style { margin: 0 auto; max-width: 98%; }
.narrow { max-width: 94%; margin-left: auto; margin-right: auto; }
.type-sm { font-size: 6.5pt; }
.type-md { font-size: 7.5pt; }
.type-lg { font-size: 8.5pt; }
.lh-tight { line-height: 1.22; }
.lh-normal { line-height: 1.35; }
.lh-loose { line-height: 1.55; }
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

_PAGE_W = 412.0
_PAGE_H = 612.0
_MARGIN = 12

_API_IMG_RE = re.compile(
    r'src=["\']/api/v1/pages/([0-9a-fA-F-]{36})/figures/([^"\']+)["\']',
    re.I,
)

_FONT_DIRS = (
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/noto",
)

_FONT_FILES = (
    "/usr/share/fonts/truetype/noto/NotoSerifDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
)

_SCAN_MAX_PX = 1400
_SCAN_JPEG_Q = 78

_CHROME_CANDIDATES = (
    "/snap/bin/chromium",
    "chromium",
    "chromium-browser",
    "google-chrome-stable",
    "google-chrome",
)


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
            frag = _rewrite_img_srcs_for_pdf(frag, project_id, page_no)
            text_html = f"<section class='page' data-page='{page_no}'>{frag}</section>"
            text_doc = _html_to_doc(text_html, mediabox)
            doc.insert_pdf(text_doc)
            text_doc.close()
            body_n += 1

    if body_n == 0:
        empty = _html_to_doc(
            "<p class='centered'>(нет страниц с текстом для выгрузки)</p>", mediabox
        )
        doc.insert_pdf(empty)
        empty.close()

    try:
        fixed = _fix_indic_copy(doc)
        doc.close()
        doc = fixed
        engine = ("chromium" if _chrome_bin() else "story") + "+copyfix"
    except Exception as exc:  # noqa: BLE001
        log.warning("indic copy-fix skipped (%s)", exc)
        engine = "chromium" if _chrome_bin() else "story"

    doc.set_metadata(
        {
            "producer": f"sanskrit_srv/{engine}",
            "creator": "Sanskrit SRV",
            "title": title or slug,
        }
    )
    tmp_out = out_path.with_suffix(f".{uuid.uuid4().hex}.tmp.pdf")
    doc.save(tmp_out.as_posix(), garbage=3, deflate=True)
    doc.close()
    tmp_out.replace(out_path)
    return out_path


def _deva_fontfile() -> str | None:
    for path in _FONT_FILES:
        if Path(path).is_file():
            return path
    for _, bundled in _bundled_font_files():
        if bundled.is_file():
            return bundled.as_posix()
    return None


def _page_text_lines(page: fitz.Page) -> list[tuple[str, fitz.Rect, float]]:
    """Clean extractable lines (no ZWNJ) with bboxes — used for invisible overlay."""
    lines: list[tuple[str, fitz.Rect, float]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans") or []
            if not spans:
                continue
            text = "".join(s.get("text", "") for s in spans)
            text = text.replace("\u200c", "").replace("\u200d", "")
            if not text.strip():
                continue
            bbox = fitz.Rect(
                min(s["bbox"][0] for s in spans),
                min(s["bbox"][1] for s in spans),
                max(s["bbox"][2] for s in spans),
                max(s["bbox"][3] for s in spans),
            )
            size = max(float(s.get("size") or 7.5) for s in spans)
            lines.append((text, bbox, size))
    return lines


def _fix_indic_copy(src: fitz.Document) -> fitz.Document:
    """Path-outline Devanagari pages + invisible clean Unicode for copy/paste."""
    font_path = _deva_fontfile()
    if not font_path:
        raise RuntimeError("no Devanagari font for copy-fix overlay")
    font = fitz.Font(fontfile=font_path)
    out = fitz.open()
    for page in src:
        sample = page.get_text()
        n_deva = sum(1 for c in sample if "\u0900" <= c <= "\u097f")
        if n_deva < 4:
            out.insert_pdf(src, from_page=page.number, to_page=page.number)
            continue
        lines = _page_text_lines(page)
        try:
            svg = page.get_svg_image(text_as_path=True)
            svg_doc = fitz.open("svg", svg.encode("utf-8"))
            pdf_bytes = svg_doc.convert_to_pdf()
            svg_doc.close()
            one = fitz.open("pdf", pdf_bytes)
        except Exception as exc:  # noqa: BLE001
            log.warning("svg path convert failed p%s (%s); keeping page", page.number, exc)
            out.insert_pdf(src, from_page=page.number, to_page=page.number)
            continue
        dest = out.new_page(width=page.rect.width, height=page.rect.height)
        dest.show_pdf_page(dest.rect, one, 0)
        one.close()
        if lines:
            tw = fitz.TextWriter(dest.rect)
            for text, bbox, size in lines:
                pos = fitz.Point(bbox.x0, bbox.y1 - 0.12 * size)
                tw.append(pos, text, font=font, fontsize=size)
            tw.write_text(dest, render_mode=3)
    return out


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


def _chrome_bin() -> str | None:
    for cand in _CHROME_CANDIDATES:
        if Path(cand).is_file():
            return cand
        found = shutil.which(cand)
        if found:
            return found
    return None


def _work_dir() -> Path:
    # Snap Chromium cannot write under many system paths; home subdir works.
    d = Path.home() / "sanskrit_pdf_work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bundled_font_files() -> list[tuple[str, Path]]:
    """Copy system Devanagari TTFs into the Chromium-writable work dir.

    Snap Chromium often cannot load file:///usr/share/fonts/…; without a real
    face it falls back to a Latin font and Devanagari 'falls apart'.
    """
    fonts_dir = _work_dir() / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    out: list[tuple[str, Path]] = []
    serif = next((p for p in _FONT_FILES if "SerifDevanagari" in p and Path(p).is_file()), None)
    sans = next((p for p in _FONT_FILES if "SansDevanagari" in p and Path(p).is_file()), None)
    pairs = [
        ("Noto Serif Devanagari", serif),
        ("Noto Sans Devanagari", sans or serif),
    ]
    for family, src in pairs:
        if not src:
            continue
        src_path = Path(src)
        if not src_path.is_file():
            continue
        dest = fonts_dir / src_path.name
        if not dest.is_file() or dest.stat().st_size != src_path.stat().st_size:
            shutil.copy2(src_path, dest)
        out.append((family, dest))
    return out


def _font_face_css() -> str:
    parts: list[str] = []
    for family, path in _bundled_font_files():
        uri = path.resolve().as_uri()
        parts.append(
            "@font-face{"
            f"font-family:\"{family}\";"
            f"src:url('{uri}');"
            "font-display:block;"
            "}"
        )
    return "\n".join(parts)


def _html_to_doc(body_html: str, mediabox: fitz.Rect) -> fitz.Document:
    chrome = _chrome_bin()
    if chrome:
        try:
            return _html_to_doc_chrome(chrome, body_html, mediabox)
        except Exception as exc:  # noqa: BLE001
            log.warning("chromium PDF failed (%s); falling back to Story", exc)
    else:
        log.warning("chromium not found; PDF text uses Story fallback")
    return _html_to_doc_story(body_html, mediabox)


def _html_to_doc_chrome(
    chrome: str, body_html: str, mediabox: fitz.Rect
) -> fitz.Document:
    """Chromium print-to-PDF: shaped Devanagari + correct copy/paste."""
    page_css = (
        f"@page{{size:{mediabox.width:.3f}pt {mediabox.height:.3f}pt;"
        f"margin:{_MARGIN}pt;}}\n"
        + _font_face_css()
        + "\n"
        + CSS
    )
    doc_html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{page_css}</style></head>"
        f"<body>{body_html}</body></html>"
    )
    work = _work_dir()
    token = uuid.uuid4().hex
    html_path = work / f"{token}.html"
    pdf_path = work / f"{token}.pdf"
    try:
        html_path.write_text(doc_html, encoding="utf-8")
        proc = subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path.as_posix()}",
                html_path.resolve().as_uri(),
            ],
            check=False,
            timeout=180,
            capture_output=True,
        )
        if proc.returncode != 0 and not pdf_path.is_file():
            err = (proc.stderr or b"").decode("utf-8", "replace")[-500:]
            raise RuntimeError(f"chromium exit {proc.returncode}: {err}")
        if not pdf_path.is_file() or pdf_path.stat().st_size < 100:
            raise RuntimeError("chromium produced no PDF")
        src = fitz.open(pdf_path.as_posix())
        # Refuse Latin-fallback junk: Devanagari pages must embed a Deva face.
        fonts = " ".join(f[3] or "" for f in src[0].get_fonts())
        if src.page_count > 0 and not re.search(r"Devanagari|Noto|FreeSerif|lohit|Nakula", fonts, re.I):
            # Allow empty/cover-like pages; only warn when body looks Devanagari-heavy.
            sample = src[0].get_text()
            if sum(1 for c in sample if "\u0900" <= c <= "\u097f") >= 8:
                src.close()
                raise RuntimeError(f"chromium PDF missing Devanagari font (got: {fonts!r})")
        mem = fitz.open()
        mem.insert_pdf(src)
        src.close()
        return mem
    finally:
        html_path.unlink(missing_ok=True)
        pdf_path.unlink(missing_ok=True)


def _font_archive() -> fitz.Archive:
    roots = [d for d in _FONT_DIRS if Path(d).is_dir()]
    return fitz.Archive(*roots) if roots else fitz.Archive()


def _html_to_doc_story(body_html: str, mediabox: fitz.Rect) -> fitz.Document:
    """Fallback: Story via DocumentWriter device (shaped glyphs; weaker ToUnicode)."""
    doc_html = (
        "<html><head><meta charset='utf-8'></head>"
        f"<body>{body_html}</body></html>"
    )
    where = mediabox + (_MARGIN, _MARGIN, -_MARGIN, -_MARGIN)
    story = fitz.Story(html=doc_html, user_css=CSS, archive=_font_archive())
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
    page = doc.new_page(width=mediabox.width, height=mediabox.height)
    rect = page.rect
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
) -> str:
    """Map /api/.../figures/name to file:// URLs Chromium can load."""
    from app.services.layout_assets import figure_file

    def repl(m: re.Match) -> str:
        name = m.group(2)
        path = figure_file(project_id, page_no, name)
        if path is None or not path.is_file():
            return m.group(0)
        return f'src="{path.resolve().as_uri()}"'

    return _API_IMG_RE.sub(repl, html)


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
