"""Tesseract digitize HTML shaping (no system tesseract required)."""
from app.services.tesseract_ocr import (
    _lines_from_plain,
    _lines_from_tsv,
    lines_to_html,
    _norm_line,
)


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


def test_lines_from_plain_skips_blanks():
    assert _lines_from_plain("क\n\nख\n") == ["क", "ख"]


def test_lines_from_tsv_joins_words():
    # level,page,block,par,line,word,left,top,width,height,conf,text
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "4\t1\t1\t1\t1\t0\t0\t0\t10\t10\t-1\t\n"
        "5\t1\t1\t1\t1\t1\t0\t0\t5\t10\t90\tशिव\n"
        "5\t1\t1\t1\t1\t2\t6\t0\t5\t10\t88\tसूत्र\n"
    )
    assert _lines_from_tsv(tsv) == ["शिव सूत्र"]
