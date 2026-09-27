"""Merge IAST lines into Russian interlinear drafts."""
from app.services.merge_iast import (
    extract_sa_iast_pairs,
    merge_iast_into_translation,
    normalize_sa,
)


def test_normalize_strips_punctuation():
    assert normalize_sa("यज्ञो वै ॥ १ ॥") == normalize_sa("यज्ञोवै")


def test_extract_pairs():
    html = """
    <article class="page-style" lang="sa">
      <p class="sa">यज्ञः</p>
      <p class="iast" lang="sa-Latn">yajñaḥ</p>
      <p class="sa">कर्म</p>
      <p class="iast" lang="sa-Latn">karma</p>
    </article>
    """
    pairs = extract_sa_iast_pairs(html)
    assert len(pairs) == 2
    assert pairs[0][1].startswith("<p")
    assert "yajñaḥ" in pairs[0][1]


def test_extract_pairs_inside_shloka_div():
    html = """
    <article>
      <div class="shloka sa">
        <p class="narrow">भीरूणामभयप्रदो</p>
        <p class="iast narrow" lang="sa-Latn">bhīrūṇāmabhayaprado</p>
        <p class="narrow indent">हृद्धाम्नि</p>
        <p class="iast narrow indent" lang="sa-Latn">hṛddhāmni</p>
      </div>
    </article>
    """
    pairs = extract_sa_iast_pairs(html)
    assert len(pairs) == 2
    assert "bhīrūṇāmabhayaprado" in pairs[0][1]
    assert "hṛddhāmni" in pairs[1][1]


def test_merge_inserts_between_sa_and_ru():
    ru = """
    <article class="page-style" lang="ru">
      <p class="sa">यज्ञः</p>
      <p class="ru tr" lang="ru">Жертва</p>
      <p class="sa">कर्म</p>
      <p class="ru tr" lang="ru">деяние</p>
    </article>
    """
    iast = """
    <article class="page-style" lang="sa">
      <p class="sa">यज्ञः</p>
      <p class="iast" lang="sa-Latn">yajñaḥ</p>
      <p class="sa">कर्म</p>
      <p class="iast" lang="sa-Latn">karma</p>
    </article>
    """
    out, stats = merge_iast_into_translation(ru, iast)
    assert stats["inserted"] == 2
    assert out.index("yajñaḥ") < out.index("Жертва")
    assert out.index("karma") < out.index("деяние")
    i_sa = out.index("यज्ञः")
    i_iast = out.index("yajñaḥ")
    i_ru = out.index("Жертва")
    assert i_sa < i_iast < i_ru


def test_merge_inside_shloka_before_ru():
    ru = """
    <article lang="ru">
      <div class="shloka sa">
        <p class="narrow">भीरूणामभयप्रदो</p>
        <p class="narrow indent">हृद्धाम्नि</p>
        <p class="ru tr">перевод строфы</p>
      </div>
    </article>
    """
    iast = """
    <article>
      <div class="shloka sa">
        <p class="narrow">भीरूणामभयप्रदो</p>
        <p class="iast narrow" lang="sa-Latn">bhīrūṇāmabhayaprado</p>
        <p class="narrow indent">हृद्धाम्नि</p>
        <p class="iast narrow indent" lang="sa-Latn">hṛddhāmni</p>
      </div>
    </article>
    """
    out, stats = merge_iast_into_translation(ru, iast)
    assert stats["inserted"] == 2
    assert "bhīrūṇāmabhayaprado" in out
    assert "hṛddhāmni" in out
    assert out.index("bhīrūṇāmabhayaprado") < out.index("हृद्धाम्नि")
    assert out.index("hṛddhāmni") < out.index("перевод строфы")


def test_merge_skips_existing_iast():
    ru = """
    <article lang="ru">
      <p class="sa">यज्ञः</p>
      <p class="iast" lang="sa-Latn">yajñaḥ</p>
      <p class="ru tr">Жертва</p>
    </article>
    """
    iast = """
    <article>
      <p class="sa">यज्ञः</p>
      <p class="iast" lang="sa-Latn">OTHER</p>
    </article>
    """
    out, stats = merge_iast_into_translation(ru, iast)
    assert stats["skipped_existing"] == 1
    assert stats["inserted"] == 0
    assert "OTHER" not in out
