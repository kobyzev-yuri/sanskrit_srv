"""Merge two iast_gloss translations; splice a single verse into the page."""
import pytest

from app.services.llm_merge import apply_merged_pairs, merge_translations

SA_4 = (
    "धीमन्दराचलवलत्परमागमाब्धे-"
    "रुल्लास्यते किमपि यत्परमामृतं तत् ।"
    "जीवच्छिवत्वमधिगन्तुममुत्र सद्भिः"
    "संचर्व्यतामविरतं परशक्तिपूतैः ॥ ४ ॥"
)
SA_3 = "शिवः शिवः ॥ ३ ॥"

GEMINI_4 = (
    "Тот (tat) некий (kim api) высший нектар (parama-amṛtam), который (yat) "
    "извлекается (ullāsyate) из океана (abdheḥ) высших писаний (parama-āgama), "
    "взбиваемого (valat) горой Мандара (mandarācala) — разумом (dhī), — "
    "пусть непрестанно (aviratam) вкушается (saṃcarvyatām) здесь (amutra) "
    "благочестивыми (sadbhiḥ), очищенными (pūtaiḥ) Высшей Силой (para-śakti), "
    "дабы обрести (adhigantum) состояние живого Шивы (jīvat-śivatvam). ॥ ४ ॥"
)
HYBRID_4 = (
    "Тот (tat) невыразимый (kim api) высший нектар (paramāmṛtam), что (yat) "
    "извлекается (ullāsyate) из океана высших агам (paramāgama-abdheḥ) "
    "[с помощью] разума (dhī), вращаемого (valat) подобно горе Мандаре (mandarācala), "
    "да смакуется (saṃcarvyatām) непрестанно (aviratam) благими (sadbhiḥ), "
    "очищенными (pūtaiḥ) Высшей Шакти (para-śakti), дабы достичь (adhigantum) "
    "в нём (amutra) состояния Шивы (śivatvam) при жизни (jīvat)."
)
RU_3 = "Шива (śivaḥ) Шива (śivaḥ). ॥ ३ ॥"

PROFESSOR_4 = (
    "Тот (tat) невыразимый (kim api - буквально “нечто [неописуемое]”) "
    "высший нектар (parama-amṛtam), что (yat) извлекается (ullāsyate) "
    "из океана высших агам (-parama-āgama-abdheḥ) [с помощью] разума (dhī-), "
    "вращаемого (-valat-) подобно горе Мандаре (-mandara-acala-), "
    "да смакуется (saṃcarvyatām) непрестанно (aviratam) благими [людьми] (sadbhiḥ), "
    "очищенными Высшей Шакти (para-śakti-pūtaiḥ), дабы достичь (adhigantum) "
    "в нём (amutra) состояния Шивы (-śivatvam) при жизни (jīvat-)! ॥ ४ ॥"
)

SOURCE = (
    '<article class="page-style" lang="sa">'
    f'<p class="sa shloka" lang="sa">{SA_3}</p>'
    f'<p class="sa shloka" lang="sa">{SA_4}</p>'
    "</article>"
)
DRAFT = (
    '<article class="page-style" lang="ru">'
    f'<p class="sa shloka" lang="sa">{SA_3}</p>'
    f'<p class="ru tr" lang="ru">{RU_3}</p>'
    f'<p class="sa shloka" lang="sa">{SA_4}</p>'
    f'<p class="ru tr" lang="ru">{GEMINI_4}</p>'
    "</article>"
)
MERGED_ONE = (
    '<article class="page-style" lang="ru">'
    f'<p class="sa shloka" lang="sa">{SA_4}</p>'
    f'<p class="ru tr" lang="ru">{HYBRID_4}</p>'
    "</article>"
)


def _assert_hybrid(ru: str) -> None:
    assert "при жизни" in ru
    assert "Шакти" in ru
    assert "агам" in ru
    assert "смакует" in ru
    assert "живого Шивы" not in ru
    assert "буквально" not in ru
    assert "писаний" not in ru


def test_splice_replaces_only_matching_verse():
    out, n = apply_merged_pairs(DRAFT, MERGED_ONE)
    assert n == 1
    assert RU_3 in out
    assert GEMINI_4 not in out
    _assert_hybrid(out)
    assert SA_3 in out and SA_4 in out


def test_splice_by_verse_number_without_full_deva():
    fragment = (
        '<article class="page-style" lang="ru">'
        '<p class="sa shloka" lang="sa">॥ ४ ॥</p>'
        f'<p class="ru tr" lang="ru">{HYBRID_4}</p>'
        "</article>"
    )
    out, n = apply_merged_pairs(DRAFT, fragment)
    assert n == 1
    assert GEMINI_4 not in out
    _assert_hybrid(out)


def test_merge_translations_uses_llm_and_splices(monkeypatch):
    def fake_prompt(user_text, **_k):
        assert "SOURCE HTML" in user_text
        assert "ЧЕРНОВИК A" in user_text
        assert "АЛЬТЕРНАТИВА B" in user_text
        assert PROFESSOR_4 in user_text
        return MERGED_ONE, "gemini:test", {"network": "gemini", "model": "test"}

    monkeypatch.setattr("app.services.llm_merge.run_text_prompt", fake_prompt)
    html, model, usage = merge_translations(
        source_html=SOURCE,
        draft_html=DRAFT,
        alt_text=PROFESSOR_4,
        style="iast_gloss",
    )
    assert model == "gemini:test"
    assert usage["network"] == "gemini"
    assert RU_3 in html
    assert GEMINI_4 not in html
    _assert_hybrid(html)
    assert "TEMPLATE" not in html
    assert "буквально" not in html


def test_nested_shloka_div_keeps_other_verses():
    draft = (
        '<article class="page-style" lang="ru">'
        '<p class="sa centered">ओं</p><p class="ru tr">Ом (oṃ).</p>'
        '<div class="shloka sa">'
        f'<p class="narrow">{SA_3}</p>'
        f'<p class="ru tr" lang="ru">{RU_3}</p>'
        "</div>"
        '<div class="shloka sa">'
        '<p class="narrow">धीमन्दराचलवलत्परमागमाब्धे-</p>'
        f'<p class="narrow indent">संचर्व्यतामविरतं परशक्तिपूतैः ॥ ४ ॥</p>'
        f'<p class="ru tr" lang="ru">{GEMINI_4}</p>'
        "</div>"
        "</article>"
    )
    out, n = apply_merged_pairs(draft, MERGED_ONE)
    assert n == 1
    assert "Ом (oṃ)" in out
    assert RU_3 in out
    assert GEMINI_4 not in out
    _assert_hybrid(out)


def test_unmatched_single_verse_does_not_wipe_page():
    draft = DRAFT
    merged = (
        '<article class="page-style" lang="ru">'
        '<p class="sa shloka">अन्यः श्लोकः ॥ ९९ ॥</p>'
        '<p class="ru tr">Чужой текст (anyaḥ).</p>'
        "</article>"
    )
    out, n = apply_merged_pairs(draft, merged)
    assert n == 0
    assert out == draft
    assert SA_3 in out and SA_4 in out


def test_merge_refuses_to_replace_page_with_one_verse(monkeypatch):
    def fake_prompt(*_a, **_k):
        html = (
            '<article class="page-style" lang="ru">'
            '<p class="sa shloka">अन्यः ॥ ९९ ॥</p>'
            '<p class="ru tr">Чужой достаточно длинный перевод (anyaḥ) для проверки.</p>'
            "</article>"
        )
        return html, "gemini:test", {"network": "gemini", "model": "test"}

    monkeypatch.setattr("app.services.llm_merge.run_text_prompt", fake_prompt)
    with pytest.raises(ValueError, match="не изменена"):
        merge_translations(
            source_html=SOURCE,
            draft_html=DRAFT,
            alt_text=PROFESSOR_4,
            style="iast_gloss",
        )


def test_merge_rejects_empty_alt():
    with pytest.raises(ValueError, match="второй"):
        merge_translations(source_html=SOURCE, draft_html=DRAFT, alt_text="нет")
