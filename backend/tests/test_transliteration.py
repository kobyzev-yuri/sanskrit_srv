"""IAST transliteration templates, HTML validation, and task routing."""
from types import SimpleNamespace

import pytest

from app.services.llm_translate import (
    looks_like_transliteration_html,
    validate_transliteration_html,
)
from app.services.translation_style import (
    STYLE_IAST_BLOCK,
    STYLE_IAST_PLAIN,
    TRANSLIT_STYLES,
    build_transliterate_messages,
    default_transliteration_settings,
    is_source_html_task,
    project_task,
)


BLOCK = """
<article class="page-style" lang="sa">
  <p class="sa shloka" lang="sa">यज्ञो वै श्रेष्ठतमं कर्म</p>
  <p class="iast" lang="sa-Latn">yajño vai śreṣṭhatamaṃ karma</p>
</article>
"""

PLAIN = """
<article class="page-style" lang="sa-Latn">
  <p class="iast" lang="sa-Latn">yajño vai śreṣṭhatamaṃ karma</p>
</article>
"""


def test_project_task_transliterate():
    tr = SimpleNamespace(settings={"task": "transliterate"})
    ru = SimpleNamespace(settings={"task": "translate"})
    dig = SimpleNamespace(settings={"task": "digitize"})
    assert project_task(tr) == "transliterate"
    assert project_task(ru) == "translate"
    assert project_task(dig) == "digitize"
    assert is_source_html_task(tr)
    assert is_source_html_task(ru)
    assert not is_source_html_task(dig)


def test_default_iast_templates():
    cfg = default_transliteration_settings()
    assert cfg["style"] == STYLE_IAST_BLOCK
    assert cfg["agreed"] is True
    assert STYLE_IAST_PLAIN in TRANSLIT_STYLES
    cfg_plain = default_transliteration_settings(style="nope")
    assert cfg_plain["style"] == STYLE_IAST_BLOCK


def test_iast_block_prompt_keeps_devanagari():
    src = '<article class="page-style" lang="sa"><p class="sa">यज्ञः</p></article>'
    cfg = default_transliteration_settings(style=STYLE_IAST_BLOCK)
    system, user = build_transliterate_messages(source_html=src, cfg=cfg)
    assert "iast_block" in system
    assert 'class="iast"' in system
    assert "Keep each Sanskrit" in system
    assert src in user
    assert src not in system


def test_iast_plain_prompt_drops_devanagari_body():
    src = '<p class="sa">कर्म</p>'
    cfg = default_transliteration_settings(style=STYLE_IAST_PLAIN)
    system, user = build_transliterate_messages(source_html=src, cfg=cfg)
    assert "iast_plain" in system
    assert "Do NOT keep Devanagari body text" in system
    assert src in user


def test_validate_iast_block_and_plain():
    assert looks_like_transliteration_html(BLOCK, style=STYLE_IAST_BLOCK)
    out = validate_transliteration_html(PLAIN, style=STYLE_IAST_PLAIN)
    assert "yajño" in out
    assert looks_like_transliteration_html(PLAIN, style=STYLE_IAST_PLAIN)


def test_plain_rejected_as_block():
    with pytest.raises(ValueError, match="Devanagari"):
        validate_transliteration_html(PLAIN, style=STYLE_IAST_BLOCK)


def test_source_is_latin_page():
    from app.services.llm_translate import source_is_latin_page

    english = (
        '<article class="page-style" lang="en">'
        "<h1>The Doctrine of Vibration</h1><p>Translated from the Sanskrit with notes.</p>"
        "</article>"
    )
    short_sa = '<article class="page-style" lang="sa"><p class="sa">यज्ञः</p></article>'
    assert source_is_latin_page(english)
    assert not source_is_latin_page(short_sa)
    assert not source_is_latin_page(BLOCK)
    raw = "yajño vai śreṣṭhatamaṃ karma\nagniḥ pūrvebhiḥ"
    out = validate_transliteration_html(raw, style=STYLE_IAST_BLOCK)
    assert "<article" in out.lower()
    assert "yajño" in out
    assert 'class="iast"' in out


def test_inner_tags_without_article_are_wrapped():
    raw = '<p class="sa">यज्ञः</p>\n<p class="iast">yajñaḥ</p>'
    out = validate_transliteration_html(raw, style=STYLE_IAST_BLOCK)
    assert "<article" in out.lower()
    assert "yajñaḥ" in out


def test_russian_translation_is_not_iast():
    html = (
        '<article class="page-style" lang="ru">'
        '<p class="sa">यज्ञः</p>'
        '<p class="ru tr" lang="ru">Жертва</p>'
        "</article>"
    )
    assert not looks_like_transliteration_html(html, style=STYLE_IAST_BLOCK)
