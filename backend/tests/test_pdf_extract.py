"""PDF kind: born-digital text vs scan (including OCR overlay on a page image)."""
from pathlib import Path

import fitz

from app.services.pdf_extract import classify_pdf


def _text_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=400, height=600)
    page.insert_text((40, 80), ("Hello Devanagari text layer. " * 8).strip())
    doc.save(path)
    doc.close()


def _scan_with_ocr_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=400, height=600)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 600), 0)
    pix.clear_with(200)
    page.insert_image(page.rect, pixmap=pix)
    page.insert_text((40, 80), ("OCR overlay characters " * 20).strip())
    doc.save(path)
    doc.close()


def test_classify_born_digital_text(tmp_path):
    pdf = tmp_path / "text.pdf"
    _text_pdf(pdf)
    info = classify_pdf(pdf)
    assert info["kind"] == "text"
    assert info["avg_chars"] >= 60
    assert info["avg_image_coverage"] < 0.30


def test_classify_scan_with_ocr_overlay(tmp_path):
    pdf = tmp_path / "scan_ocr.pdf"
    _scan_with_ocr_pdf(pdf)
    info = classify_pdf(pdf)
    assert info["kind"] == "scan"
    assert info["avg_chars"] >= 60
    assert info["avg_image_coverage"] >= 0.30
