"""Build a downloadable PDF from project page HTML (PyMuPDF Story).

Visual language: Shaivite — vibhuti ash, rudraksha brown, tripundra rules, ॐ.
Modes: text (HTML only) | interleave (scan page then HTML for each source page).
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import fitz

from app.services.storage import ensure_dirs

# Cream parchment · book measure · typography classes from LLM
CSS = """
body {
  font-family: "Noto Serif Devanagari", "Noto Sans Devanagari", "FreeSerif", serif;
  font-size: 10pt;
  line-height: 1.5;
  color: #1a1814;
  background-color: #f7f2e8;
}
.cover { text-align: center; margin: 0.6em 0 0.2em; }
.cover-mantra {
  font-family: "Noto Serif Devanagari", "Noto Sans Devanagari", serif;
  font-size: 13pt; color: #3d2914; margin: 0.4em 0 0.35em;
}
.cover-title { font-size: 17pt; font-weight: 700; color: #1a1814; margin: 0.5em 0 0.2em; line-height: 1.25; }
.cover-title-sa {
  font-family: "Noto Serif Devanagari", "Noto Sans Devanagari", serif;
  font-size: 13pt; color: #5c3d2e; margin: 0.1em 0 0.55em;
}
.cover-mark {
  font-family: "Noto Serif Devanagari", "Noto Sans Devanagari", serif;
  font-size: 10pt; color: #6b6560; margin: 0.4em 0;
}
.cover-brand { font-size: 8.5pt; color: #6b6560; margin-top: 0.9em; }
.tri-line {
  margin: 0.12em auto; width: 38%; height: 0; border: none; border-top: 1.15pt solid #5a5550;
}
.tri-line.mid { border-top-color: #8b3a2a; border-top-width: 1.6pt; width: 34%; margin: 0.18em auto; }
.page, .page-style { margin: 0 auto; max-width: 92%; }
.narrow { max-width: 78%; margin-left: auto; margin-right: auto; }
.type-sm { font-size: 9pt; }
.type-md { font-size: 10pt; }
.type-lg { font-size: 11.5pt; }
.lh-tight { line-height: 1.32; }
.lh-normal { line-height: 1.5; }
.lh-loose { line-height: 1.78; }
.compact p { margin: 0.25em 0; }
.indent { text-indent: 1.4em; }
h1 {
  text-align: center; font-size: 12.5pt; margin: 0.15em 0 0.55em; color: #3d2914; line-height: 1.35;
}
h1.sa, .sa, span.sa, p.sa, footer.sa {
  font-family: "Noto Serif Devanagari", "Noto Sans Devanagari", serif;
}
.shloka, p.shloka {
  text-align: center; margin: 0.4em auto; max-width: 85%; line-height: inherit; color: #1a1814;
}
.running-head { text-align: center; font-size: 9pt; color: #6b6560; margin: 0 0 0.5em; }
.centered { text-align: center; }
.page-num { text-align: center; font-size: 9pt; color: #6b6560; margin-top: 0.8em; }
.page-break { page-break-before: always; }
footer, .footer {
  text-align: center; margin-top: 0.8em; font-size: 9pt; color: #6b6560;
}
figure.page-figure { text-align: center; margin: 0.6em 0; }
figure.page-figure img { max-width: 90%; }
table { width: 100%; border-collapse: collapse; margin: 0.45em 0; }
td, th { border: 0.6pt solid #c4b8a8; padding: 0.12em 0.28em; }
th { background: #efe6d8; color: #3d2914; }
.scan-plate { text-align: center; margin: 0; }
.scan-plate img { max-width: 100%; }
"""

_API_IMG_RE = re.compile(
    r'src=["\']/api/v1/pages/([0-9a-fA-F-]{36})/figures/([^"\']+)["\']',
    re.I,
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
    """pages: list of (page_no, html_fragment, scan_path|None).

    mode=text — HTML only (default).
    mode=interleave — for each page: full scan plate, then HTML (скан ‖ текст).
    """
    mode = (mode or "text").strip().lower()
    if mode not in ("text", "interleave"):
        mode = "text"

    cover = _cover_html(title, title_sa)
    parts = [cover]
    body_n = 0
    for page_no, html, scan_path in pages:
        frag = (html or "").strip()
        if not frag and mode == "text":
            continue
        if mode == "interleave" and scan_path and Path(scan_path).is_file():
            parts.append(
                "<div class='page-break'></div>"
                f"<section class='scan-plate' data-page='{page_no}'>"
                f"<img src='{_file_uri(Path(scan_path))}' />"
                "</section>"
            )
            body_n += 1
        if frag:
            frag = _rewrite_img_srcs_for_pdf(frag, project_id, page_no)
            parts.append(
                f"<div class='page-break'></div>"
                f"<section class='page' data-page='{page_no}'>{frag}</section>"
            )
            body_n += 1
    if body_n == 0:
        parts.append(
            "<div class='page-break'></div>"
            "<p class='centered'>(нет страниц с текстом для выгрузки)</p>"
        )

    doc_html = (
        "<html><head><meta charset='utf-8'></head>"
        f"<body>{''.join(parts)}</body></html>"
    )
    out_dir = ensure_dirs() / "exports" / str(project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-interleave" if mode == "interleave" else ""
    out_path = out_dir / f"{slug}{suffix}.pdf"

    mediabox = fitz.paper_rect("a5")
    where = mediabox + (30, 34, -30, -32)
    story = fitz.Story(html=doc_html, user_css=CSS)
    writer = fitz.DocumentWriter(out_path.as_posix())
    more = True
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()

    _decorate_shaiva_pages(out_path)
    return out_path


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _rewrite_img_srcs_for_pdf(html: str, project_id: uuid.UUID, page_no: int) -> str:
    """Map /api/.../figures/name to file:// under storage."""
    from app.services.layout_assets import figure_file

    def repl(m: re.Match) -> str:
        name = m.group(2)
        path = figure_file(project_id, page_no, name)
        if path is None:
            return m.group(0)
        return f'src="{_file_uri(path)}"'

    return _API_IMG_RE.sub(repl, html)


def _cover_html(title: str, title_sa: str | None) -> str:
    sa = (title_sa or "").strip()
    sa_block = f"<p class='cover-title-sa sa'>{_esc(sa)}</p>" if sa else ""
    tri = (
        '<hr class="tri-line" />'
        '<hr class="tri-line mid" />'
        '<hr class="tri-line" />'
    )
    return f"""
<section class="cover">
  <p class="cover-mantra sa">ॐ नमः शिवाय</p>
  {tri}
  <p class="cover-mark sa">॥ शिवार्पणम् ॥</p>
  <h1 class="cover-title">{_esc(title)}</h1>
  {sa_block}
  {tri}
  <p class="cover-brand">Sanskrit SRV · शिवभक्त-ग्रन्थ</p>
</section>
"""


def _deva_font() -> str | None:
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSerifDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/noto/NotoSerifDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if Path(p).is_file():
            return p
    return None


def _decorate_shaiva_pages(path: Path) -> None:
    """Tripundra header + Om footer on every page (drawn in PDF space)."""
    doc = fitz.open(path.as_posix())
    ash = (0.35, 0.33, 0.31)
    kumkum = (0.55, 0.22, 0.16)
    ink = (0.28, 0.22, 0.16)
    fontfile = _deva_font()
    tmp = path.with_suffix(".shaiva.pdf")
    for i, page in enumerate(doc):
        r = page.rect
        band = fitz.Rect(r.x0, r.y0, r.x1, 28)
        page.draw_rect(band, color=None, fill=(0.97, 0.95, 0.91), overlay=True)
        band_b = fitz.Rect(r.x0, r.y1 - 26, r.x1, r.y1)
        page.draw_rect(band_b, color=None, fill=(0.97, 0.95, 0.91), overlay=True)

        if i > 0:
            y0 = 12.0
            x0, x1 = r.x0 + 52, r.x1 - 52
            page.draw_line(fitz.Point(x0, y0), fitz.Point(x1, y0), color=ash, width=0.7)
            page.draw_line(fitz.Point(x0, y0 + 4), fitz.Point(x1, y0 + 4), color=kumkum, width=1.1)
            page.draw_line(fitz.Point(x0, y0 + 8), fitz.Point(x1, y0 + 8), color=ash, width=0.7)

        footer_y = r.y1 - 16
        if fontfile:
            page.insert_font(fontname="deva", fontfile=fontfile)
            tw = (
                fitz.get_text_length("ॐ", fontfile=fontfile, fontsize=9)
                if hasattr(fitz, "get_text_length")
                else 10
            )
            page.insert_text(
                fitz.Point(r.width / 2 - tw / 2, footer_y),
                "ॐ",
                fontsize=9,
                fontname="deva",
                color=ink,
            )
        page.insert_text(
            fitz.Point(r.x1 - 36, footer_y),
            str(i + 1),
            fontsize=8,
            fontname="helv",
            color=ash,
        )
    doc.save(tmp.as_posix(), garbage=3, deflate=True)
    doc.close()
    tmp.replace(path)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
