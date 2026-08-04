"""Extract page PNGs / native text from PDF via PyMuPDF."""
from __future__ import annotations

import html
import re
from pathlib import Path

import fitz  # PyMuPDF

from app.services.storage import page_png_path

# Avg extractable chars on sampled pages above this → born-digital / text PDF (skip LLM).
TEXT_PDF_AVG_CHARS = 60


def pdf_page_count(pdf_path: Path) -> int:
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def classify_pdf(pdf_path: Path, sample_pages: int = 8) -> dict:
    """Return {kind: 'scan'|'text', avg_chars, samples, page_count}.

    Text PDFs have a real text layer; scans are image-only (or near-empty text).
    """
    with fitz.open(pdf_path) as doc:
        total = doc.page_count
        if total <= 0:
            return {"kind": "scan", "avg_chars": 0, "samples": [], "page_count": 0}
        step = max(1, total // sample_pages)
        indices = list(range(0, total, step))[:sample_pages]
        if total - 1 not in indices:
            indices.append(total - 1)
        samples: list[dict] = []
        for i in indices:
            page = doc.load_page(i)
            text = page.get_text("text") or ""
            # ignore tiny OCR garbage layers
            chars = len(re.sub(r"\s+", "", text))
            samples.append({"page": i + 1, "chars": chars})
        avg = sum(s["chars"] for s in samples) / max(1, len(samples))
        kind = "text" if avg >= TEXT_PDF_AVG_CHARS else "scan"
        return {
            "kind": kind,
            "avg_chars": round(avg, 1),
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
