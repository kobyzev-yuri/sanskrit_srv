"""Pair Sanskrit / Russian blocks from translation HTML."""
from app.services.translation_rows import html_blocks, pair_sa_ru, translation_rows


def test_pair_interlinear():
    html = """
    <article class="page-style" lang="ru">
      <p class="sa shloka" lang="sa">यज्ञो वै श्रेष्ठतमं कर्म</p>
      <p class="ru tr" lang="ru">Жертва — наилучшее деяние</p>
      <p class="sa shloka" lang="sa">यज्ञाश्वः</p>
      <p class="ru tr" lang="ru">жертвенный (यज्ञ) конь (अश्व)</p>
    </article>
    """
    rows = translation_rows([(3, html)])
    assert rows == [
        (3, "यज्ञो वै श्रेष्ठतमं कर्म", "Жертва — наилучшее деяние"),
        (3, "यज्ञाश्वः", "жертвенный (यज्ञ) конь (अश्व)"),
    ]


def test_unpaired_russian():
    html = '<p class="ru note">Только комментарий</p>'
    assert pair_sa_ru(html_blocks(html), 1) == [(1, "", "Только комментарий")]


def test_empty_page_skipped():
    assert translation_rows([(1, ""), (2, "   ")]) == []
