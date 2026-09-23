"""Voice memory: lexical few-shot selection and style-card distill."""
from types import SimpleNamespace

from app.services.voice_memory import (
    build_voice_block,
    distill_style_card,
    empty_style_card,
    format_style_card,
    gloss_pairs,
    parse_batch_examples,
    select_examples,
)


def _ex(sa: str, ru: str, **kw):
    return SimpleNamespace(source_sa=sa, target_ru=ru, verse_key=kw.get("verse_key"), **kw)


SA_4 = (
    "धीमन्दराचलवलत्परमागमाब्धे-"
    "रुल्लास्यते किमपि यत्परमामृतं तत् ।"
    "जीवच्छिवत्वमधिगन्तुममुत्र सद्भिः"
    "संचर्व्यतामविरतं परशक्तिपूतैः ॥ ४ ॥"
)
RU_4 = (
    "Тот (tat) невыразимый (kim api) высший нектар (paramāmṛtam), что (yat) "
    "извлекается (ullāsyate) из океана высших агам (paramāgama-abdheḥ) "
    "[с помощью] разума (dhī), да смакуется (saṃcarvyatām) непрестанно (aviratam) "
    "благими (sadbhiḥ), очищенными (pūtaiḥ) Высшей Шакти (para-śakti), "
    "дабы достичь (adhigantum) в нём (amutra) состояния Шивы (śivatvam) при жизни (jīvat)."
)
SA_OTHER = "शिवः शिवः ॥ ३ ॥"
RU_OTHER = "Шива (śivaḥ) Шива (śivaḥ)."


def test_gloss_pairs_extract_iast():
    pairs = gloss_pairs(RU_4)
    iasts = {iast for _ru, iast in pairs}
    assert "kimapi" in iasts or "kim api".replace(" ", "") in {x.replace(" ", "") for x in iasts}
    assert any("śakti" in i or "sakti" in i for i in iasts) or any(
        "śakti" in i for _r, i in pairs
    )


def test_select_examples_prefers_overlapping_deva():
    bank = [
        _ex(SA_OTHER, RU_OTHER),
        _ex(SA_4, RU_4),
    ]
    source = f'<article><p class="sa">{SA_4}</p></article>'
    picked = select_examples(bank, source_html=source, top_k=1)
    assert len(picked) == 1
    assert "परमामृत" in picked[0].source_sa or "परमागमा" in picked[0].source_sa


def test_distill_lexicon_from_examples():
    card = distill_style_card(
        [_ex(SA_4, RU_4)],
        domain="kashmir_shaivism",
        existing=empty_style_card(domain="kashmir_shaivism"),
    )
    assert card["domain"] == "kashmir_shaivism"
    lex = card["lexicon"]
    assert any("āgama" in k or "agama" in k for k in lex) or any(
        "шакти" in v.lower() or "агама" in v.lower() for v in lex.values()
    )
    assert "Шакти" in lex.values() or any("Шакти" in v for v in lex.values())


def test_parse_batch_sa_ru_blocks():
    text = f"""SA:
{SA_4}
RU:
{RU_4}
===
SA:
{SA_OTHER}
RU:
{RU_OTHER}
"""
    pairs = parse_batch_examples(text)
    assert len(pairs) == 2
    assert "परमामृत" in pairs[0][0] or "परमागमा" in pairs[0][0]
    assert "невыразимый" in pairs[0][1]


def test_voice_block_mentions_name_and_example():
    voice = SimpleNamespace(
        display_name="Профессор X",
        style_card={
            "domain": "kashmir_shaivism",
            "lexicon": {"āgama": "агама", "śakti": "Шакти"},
            "grammar": ["оптатив → да …"],
            "phrase": ["без «буквально…»"],
            "prefer": "grammar_and_terms_over_fluency",
        },
    )
    block = build_voice_block(voice, [_ex(SA_4, RU_4, verse_key="4")])
    assert "Профессор X" in block
    assert "āgama" in block
    assert "ЭТАЛОНЫ" in block
    assert "невыразимый" in block


def test_format_style_card_domain_soft_gate():
    text = format_style_card(
        empty_style_card(domain="kashmir_shaivism"),
        display_name="A",
    )
    assert "кашмир" in text.lower() or "kashmir" in text.lower() or "Домен" in text
