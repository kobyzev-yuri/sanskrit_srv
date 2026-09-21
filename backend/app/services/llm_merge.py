"""Merge two Russian translations of the same Sanskrit page/verse."""
from __future__ import annotations

import re
from typing import Any

from app.services.html_chunks import split_top_level_blocks, unwrap_article
from app.services.llm_translate import run_text_prompt, validate_translation_html
from app.services.translation_style import NOTES_MAX

_CLASS_RE = re.compile(r"""\bclass=["']([^"']*)["']""", re.I)
_DEVA_RE = re.compile(r"[\u0900-\u097F]+")
_VERSE_RE = re.compile(r"॥\s*([०-९0-9]+)\s*॥")
_DIGIT = str.maketrans("०१२३४५६७८९", "0123456789")
MIN_ALT = 8

MERGE_SYSTEM = """Ты склеиваешь ДВА русских перевода одной санскритской страницы в ОДИН.

Дано:
1) SOURCE HTML — выверенный деванагари.
2) ЧЕРНОВИК A — обычно Gemini.
3) АЛЬТЕРНАТИВА B — обычно эксперт / профессор (HTML или простой текст; может быть одна шлока).

ЖЁСТКИЙ ПОРЯДОК ПРЕДПОЧТЕНИЙ:
1. Покрытие: каждое слово оригинала — русское слово с тегом (IAST). Добавленное только в [квадратных скобках].
2. Грамматика: падеж, число, наклонение. Оптатив/императив 3 л. (saṃcarvyatām) → «да …», не изъявительное «вкушается».
3. Сложения: члены по отдельности (jīvat + śivatvam = «состояние Шивы при жизни», НЕ «живой Шива»).
4. Термины кашмирского шиваизма, если корень это позволяет: āgama → агама (не «писание»), śakti → Шакти (не «Сила»), śivatva → состояние Шивы.
5. Читаемое русское предложение. Не печатай «буквально…», таблицы пратипадамов и ход рассуждений.
6. При равной грамматике бери более естественную русскую связку (часто A).

Не копируй целиком A или B. Не усредняй синонимы в ущерб грамматике.
Деванагари в <p class="sa …"> копируй из SOURCE символ в символ.
Выход: ТОЛЬКО HTML <article class="page-style" lang="ru">…</article>.
Если B — одна шлока, выведи article только с этой парой sa + ru tr. Остальное страница не трогай.
"""


def _classes(block: str) -> set[str]:
    m = _CLASS_RE.search(block or "")
    if not m:
        return set()
    return {c.lower() for c in m.group(1).split()}


def _is_sa(block: str) -> bool:
    c = _classes(block)
    return ("sa" in c or "shloka" in c) and "ru" not in c and "iast" not in c


def _is_ru(block: str) -> bool:
    return "ru" in _classes(block)


def _deva_key(text: str) -> str:
    return "".join(_DEVA_RE.findall(text or ""))


def _verse_key(text: str) -> str:
    m = _VERSE_RE.search(text or "")
    if not m:
        return ""
    return m.group(1).translate(_DIGIT)


def apply_merged_pairs(draft_html: str, merged_html: str) -> tuple[str, int]:
    """Replace Russian of matching Devanagari blocks; keep the rest of the draft.

    Returns (html, n_replaced). If nothing matched, returns merged_html as a full page.
    """
    draft = (draft_html or "").strip()
    merged = (merged_html or "").strip()
    if not merged:
        return draft, 0
    open_tag, inner, close_tag = unwrap_article(draft)
    blocks = split_top_level_blocks(inner) if inner.strip() else split_top_level_blocks(draft)
    if not blocks:
        return merged, 0

    _m_open, m_inner, _m_close = unwrap_article(merged)
    m_blocks = split_top_level_blocks(m_inner) if m_inner.strip() else split_top_level_blocks(merged)

    by_deva: dict[str, str] = {}
    by_verse: dict[str, str] = {}
    by_prefix: dict[str, str | None] = {}
    i = 0
    while i < len(m_blocks):
        block = m_blocks[i]
        ru = ""
        if i + 1 < len(m_blocks) and _is_ru(m_blocks[i + 1]):
            ru = m_blocks[i + 1]
        if ru and (_is_sa(block) or _deva_key(block)):
            d = _deva_key(block)
            v = _verse_key(block) or _verse_key(ru)
            if d:
                by_deva[d] = ru
                pref = d[:24]
                if len(d) >= 16:
                    by_prefix[pref] = None if pref in by_prefix else ru
            if v:
                by_verse[v] = ru
            i += 2
            continue
        i += 1

    def lookup(sa_block: str) -> str:
        d = _deva_key(sa_block)
        if d and d in by_deva:
            return by_deva[d]
        v = _verse_key(sa_block)
        if v and v in by_verse:
            return by_verse[v]
        if d and len(d) >= 16:
            hit = by_prefix.get(d[:24])
            if hit:
                return hit
        return ""

    out: list[str] = []
    replaced = 0
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if _is_sa(block) or (_deva_key(block) and not _is_ru(block)):
            ru_new = lookup(block)
            has_ru = i + 1 < len(blocks) and _is_ru(blocks[i + 1])
            if ru_new:
                out.append(block)
                out.append(ru_new)
                i += 2 if has_ru else 1
                replaced += 1
                continue
        out.append(block)
        i += 1

    if replaced == 0:
        return merged, 0
    open_tag = open_tag or '<article class="page-style" lang="ru">'
    close_tag = close_tag or "</article>"
    body = "".join(out)
    if not body.strip().startswith("<article"):
        body = f"{open_tag}\n{body}\n{close_tag}"
    return body, replaced


def _alt_looks_single_verse(alt: str) -> bool:
    text = alt or ""
    if len(text) > 2500:
        return False
    return bool(_VERSE_RE.search(text)) or text.count("\n") < 8


def merge_translations(
    *,
    source_html: str,
    draft_html: str,
    alt_text: str,
    style: str = "iast_gloss",
) -> tuple[str, str, dict[str, Any]]:
    source = (source_html or "").strip()
    draft = (draft_html or "").strip()
    alt = (alt_text or "").strip()
    if len(alt) < MIN_ALT:
        raise ValueError("Нужен второй перевод (хотя бы одна шлока)")
    if not source:
        raise ValueError("Нет выверенного санскрита на этой странице")
    if not draft:
        raise ValueError("Нет текущего черновика перевода")

    user_parts = [
        f"Шаблон перевода: {style}.",
        "SOURCE HTML:\n" + source[:40000],
        "ЧЕРНОВИК A:\n" + draft[:40000],
        "АЛЬТЕРНАТИВА B:\n" + alt[:NOTES_MAX],
    ]
    if _alt_looks_single_verse(alt):
        user_parts.append(
            "B похож на одну шлоку. Выведи <article> только с этой парой "
            "(деванагари из SOURCE + слитый русский). Остальные шлоки не повторяй."
        )
    text, model, usage = run_text_prompt(
        "\n\n".join(user_parts),
        system=MERGE_SYSTEM,
    )
    merged = validate_translation_html(text, source_html=source)
    spliced, n = apply_merged_pairs(draft, merged)
    if n:
        return spliced, model, usage
    return merged, model, usage
