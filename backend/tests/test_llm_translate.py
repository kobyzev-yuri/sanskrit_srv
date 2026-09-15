"""Translation HTML validation — reject ox-alpha chain-of-thought."""
import pytest

from app.services.llm_translate import looks_like_translation_html, validate_translation_html


GOOD = """
<article class="page-style" lang="ru">
  <p class="sa shloka" lang="sa">यज्ञो वै श्रेष्ठतमं कर्म</p>
  <p class="ru tr" lang="ru">Жертва — наилучшее деяние</p>
</article>
"""

COT = """
<article class="page-style" lang="ru"> … </article>
2. Keep Devanagari exactly as in source
Let me analyze the source HTML and produce the translation.
"""


def test_accepts_interlinear_russian():
    out = validate_translation_html(GOOD)
    assert "Жертва" in out
    assert "यज्ञो" in out
    assert looks_like_translation_html(GOOD)


def test_rejects_reasoning_dump():
    with pytest.raises(ValueError):
        validate_translation_html(COT)
    assert not looks_like_translation_html(COT)


def test_rejects_english_plan_inside_article():
    html = (
        '<article class="page-style" lang="ru">'
        "<p>Let me analyze the source HTML. Keep Devanagari exactly as in source</p>"
        "</article>"
    )
    with pytest.raises(ValueError, match="reasoning"):
        validate_translation_html(html)


def test_accepts_html_after_thinking_preamble():
    raw = "Let me analyze the source HTML and produce the translation.\n" + GOOD
    out = validate_translation_html(raw)
    assert "Жертва" in out
    assert "Let me analyze" not in out


def test_rejects_ellipsis_stub():
    stub = '<article class="page-style" lang="ru"> … </article>'
    with pytest.raises(ValueError):
        validate_translation_html(stub)


def test_blank_source_allows_empty_article():
    src = '<article class="page-style" lang="sa">\n</article>'
    out = '<article class="page-style" lang="ru">\n</article>'
    assert validate_translation_html(out, source_html=src) == out
    assert looks_like_translation_html(out, src)
    assert not looks_like_translation_html(out)


def test_rejects_thinking_sliced_from_quoted_p_tag():
    """CoT quotes class names; must not treat that as the page."""
    raw = (
        'Do not merge several pādas into one Russian paragraph unless source is one prose block.\n'
        '<p class="sa shloka" lang="sa">, then immediate next block '
        '<p class="ru tr" lang="ru">. For verses, each verse gets one Sanskrit block.\n'
        "Hmm. Let me think about what's most natural for these translation tasks. "
        'The instruction "Keep verse numbers on the Sanskrit line."'
    )
    with pytest.raises(ValueError):
        validate_translation_html(raw)
    assert not looks_like_translation_html(raw)
