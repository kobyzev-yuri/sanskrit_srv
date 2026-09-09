"""Extract page PNGs / native text from PDF via PyMuPDF."""
from __future__ import annotations

import html
import re
from pathlib import Path

import fitz  # PyMuPDF

from app.services.storage import page_png_path

# Avg extractable chars on sampled pages above this → born-digital / text PDF (skip LLM).
TEXT_PDF_AVG_CHARS = 60
# A page-sized raster (scan / photo) even with an OCR text overlay → treat as scan.
SCAN_IMAGE_COVERAGE = 0.30


def pdf_page_count(pdf_path: Path) -> int:
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def _page_image_coverage(page) -> float:
    """Largest image-block area as a fraction of the page. 1.0 ≈ full-page plate."""
    pw = abs(page.rect.width) or 1.0
    ph = abs(page.rect.height) or 1.0
    page_area = pw * ph
    best = 0.0
    try:
        blocks = (page.get_text("dict") or {}).get("blocks") or []
    except Exception:
        blocks = []
    for block in blocks:
        if block.get("type") != 1:
            continue
        bbox = block.get("bbox") or [0, 0, 0, 0]
        area = abs((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
        best = max(best, area / page_area)
    if best >= SCAN_IMAGE_COVERAGE:
        return best
    try:
        images = page.get_images(full=True) or []
    except Exception:
        images = []
    for img in images:
        xref = img[0]
        try:
            info = page.parent.extract_image(xref)
        except Exception:
            continue
        w = int(info.get("width") or 0)
        h = int(info.get("height") or 0)
        if w >= 400 and h >= 400:
            return max(best, 0.5)
    return best


def classify_pdf(pdf_path: Path, sample_pages: int = 8) -> dict:
    """Return {kind: 'scan'|'text', avg_chars, avg_image_coverage, samples, page_count}.

    Text PDFs have a real text layer and no page-sized raster.
    Scans are image-only, or a photographed page with an OCR overlay.
    """
    with fitz.open(pdf_path) as doc:
        total = doc.page_count
        if total <= 0:
            return {
                "kind": "scan",
                "avg_chars": 0,
                "avg_image_coverage": 0,
                "samples": [],
                "page_count": 0,
            }
        step = max(1, total // sample_pages)
        indices = list(range(0, total, step))[:sample_pages]
        if total - 1 not in indices:
            indices.append(total - 1)
        samples: list[dict] = []
        for i in indices:
            page = doc.load_page(i)
            text = page.get_text("text") or ""
            chars = len(re.sub(r"\s+", "", text))
            coverage = _page_image_coverage(page)
            samples.append(
                {
                    "page": i + 1,
                    "chars": chars,
                    "image_coverage": round(coverage, 3),
                }
            )
        avg = sum(s["chars"] for s in samples) / max(1, len(samples))
        avg_cov = sum(s["image_coverage"] for s in samples) / max(1, len(samples))
        if avg_cov >= SCAN_IMAGE_COVERAGE:
            kind = "scan"
        else:
            kind = "text" if avg >= TEXT_PDF_AVG_CHARS else "scan"
        return {
            "kind": kind,
            "avg_chars": round(avg, 1),
            "avg_image_coverage": round(avg_cov, 3),
            "samples": samples,
            "page_count": total,
        }


def extract_pages(
    pdf_path: Path,
    project_id,
    extract_from: int = 1,
    extract_to: int | None = None,
    dpi: int = 150,
) -> list[int]:
    """Render pages [extract_from, extract_to] inclusive (1-based). Returns page numbers written."""
    written: list[int] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(pdf_path) as doc:
        total = doc.page_count
        start = max(1, extract_from)
        end = min(total, extract_to or total)
        if start > end:
            return written
        for page_no in range(start, end + 1):
            page = doc.load_page(page_no - 1)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out = page_png_path(project_id, page_no)
            pix.save(out.as_posix())
            written.append(page_no)
    return written


def extract_page_text_html(pdf_path: Path, page_no: int) -> str:
    """Native text layer → simple HTML (no LLM). page_no is 1-based."""
    with fitz.open(pdf_path) as doc:
        if page_no < 1 or page_no > doc.page_count:
            return seed_html(page_no)
        page = doc.load_page(page_no - 1)
        text = (page.get_text("text") or "").strip()
    if not text:
        return seed_html(page_no)
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        blocks = [ln.strip() for ln in text.splitlines() if ln.strip()]
    body = "\n".join(
        f'  <p class="sa" lang="sa">{html.escape(b).replace(chr(10), "<br>")}</p>' for b in blocks
    )
    return f'<article class="page" data-page="{page_no}">\n{body}\n</article>\n'


def seed_html(page_no: int) -> str:
    return (
        f'<article class="page" data-page="{page_no}">\n'
        f'  <p class="sa" lang="sa"></p>\n'
        f'  <!-- edit Devanagari draft here -->\n'
        f"</article>\n"
    )
