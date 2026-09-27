"""Tesseract digitize HTML shaping (no system tesseract required)."""
from app.services.tesseract_ocr import lines_to_html, _norm_line


def test_norm_line_quotes_and_nfc():
    assert _norm_line("‘शिव’") == "'शिव'"
    assert "ऽ" in _norm_line("अपानेऽस्तं")


def test_lines_to_html_one_p_per_line():
    html = lines_to_html(["काश्मीरग्रन्थावली", "प्रथमखण्डम्"], page_no=3)
    assert 'data-page="3"' in html
    assert html.count('<p class="sa"') == 2
    assert "काश्मीरग्रन्थावली" in html
    assert "<script" not in html


def test_lines_to_html_escapes():
    html = lines_to_html(['a <b> & "x"'], page_no=1)
    assert "&lt;b&gt;" in html
    assert "&amp;" in html
