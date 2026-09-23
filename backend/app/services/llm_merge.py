"""Merge two Russian translations of the same Sanskrit page/verse."""
from __future__ import annotations

import re
from typing import Any

from app.services.llm_translate import run_text_prompt, validate_translation_html
from app.services.translation_style import NOTES_MAX

_CLASS_RE = re.compile(r"""\bclass=["']([^"']*)["']""", re.I)
_DEVA_RE = re.compile(r"[\u0900-\u097F]+")
_VERSE_RE = re.compile(r"॥\s*([०-९0-9]+)\s*॥")
_DIGIT = str.maketrans("०१२३४५६७८९", "0123456789")
_IAST_LATIN_RE = re.compile(
    r"[A-Za-zāīūṛṝḷḹṅñṭḍṇśṣḥṃṁĀĪŪṚṜḶḸṄÑṬḌṆŚṢḤṂṀ]{3,}"
)
# Other Brahmic blocks that sometimes leak into «Devanagari» lines (e.g. Bengali র).
_NON_DEVA_INDIC_RE = re.compile(
    r"[\u0980-\u09FF\u0A00-\u0A7F\u0A80-\u0AFF\u0B00-\u0B7F\u0B80-\u0BFF"
    r"\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F]"
)
_P_RE = re.compile(r"<p(\s[^>]*)?>(.*?)</p>", re.I | re.S)
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
КРИТИЧНО — ДЕВАНАГАРИ:
- В каждом <p class="sa …"> копируй текст ТОЛЬКО из SOURCE HTML, символ в символ.
- ЗАПРЕЩЕНО писать IAST / латиницу внутри санскритских <p class="sa"> (никаких mārgaḥ, svayoginī и т.п. в sa-блоках).
- IAST допустим ТОЛЬКО в русских строках <p class="ru …"> внутри скобок (…).
Выход: ТОЛЬКО HTML <article class="page-style" lang="ru">…</article>.
Если B — одна шлока, выведи article только с этой парой sa + ru tr. Остальное страница не трогай.
"""


def _classes(block: str) -> set[str]:
    m = _CLASS_RE.search(block or "")
    if not m:
        return set()
    return {c.lower() for c in m.group(1).split()}


def _deva_key(text: str) -> str:
    return "".join(_DEVA_RE.findall(text or ""))


def _verse_key(text: str) -> str:
    m = _VERSE_RE.search(text or "")
    if not m:
        return ""
    return m.group(1).translate(_DIGIT)


def _paragraphs(html: str) -> list[tuple[str, str, str]]:
    return [(m.group(0), m.group(1) or "", m.group(2) or "") for m in _P_RE.finditer(html or "")]


def _tag_classes(attrs: str) -> set[str]:
    return _classes(f"<p{attrs}>")


def _is_ru_attrs(attrs: str) -> bool:
    return "ru" in _tag_classes(attrs)


def ru_count(html: str) -> int:
    return sum(1 for _full, attrs, _inner in _paragraphs(html) if _is_ru_attrs(attrs))


def _sa_polluted(inner: str) -> bool:
    """True if a Sanskrit line has Latin/IAST or non-Devanagari Indic letters."""
    text = re.sub(r"<[^>]+>", "", inner or "")
    if _IAST_LATIN_RE.search(text):
        return True
    if _NON_DEVA_INDIC_RE.search(text):
        return True
    return False


def _looks_like_multiline_sa(inner: str) -> bool:
    """One <p> holding several pādas (common in translation drafts)."""
    if re.search(r"<br\s*/?>", inner or "", re.I):
        return True
    # Several Devanagari chunks separated by punctuation / newlines
    parts = [p for p in re.split(r"[\n।॥]+", re.sub(r"<[^>]+>", "\n", inner or "")) if _deva_key(p)]
    return len(parts) >= 2


def _join_source_verse_block(
    src_paras: list[tuple[str, str, str]],
    end_idx: int,
    used: set[int],
) -> str:
    """Join pādas from the start of the verse up to end_idx (the numbered line)."""
    start = end_idx
    while start > 0 and start - 1 not in used and not _verse_key(src_paras[start - 1][2]):
        start -= 1
    for i in range(start, end_idx + 1):
        used.add(i)
    parts = [src_paras[i][2].strip() for i in range(start, end_idx + 1)]
    return "<br>".join(parts)


def restore_sa_from_source(draft_html: str, source_html: str) -> str:
    """Replace Sanskrit <p> bodies in the draft with matching SOURCE Devanagari.

    Never leave IAST / foreign Indic scripts inside sa-lines after merge.
    Prefer 1:1 order when SA paragraph counts match; else verse / Devanagari overlap.
    Multi-pāda draft blocks (with <br>) are restored as a joined verse block — never
    replaced by only the last numbered pāda.
    """
    draft = draft_html or ""
    source = source_html or ""
    if not draft.strip() or not source.strip():
        return draft

    src_paras: list[tuple[str, str, str]] = []
    for full, attrs, inner in _paragraphs(source):
        classes = _tag_classes(attrs)
        if "ru" in classes or "tr" in classes or "iast" in classes:
            continue
        if "sa" in classes or "shloka" in classes or _deva_key(inner):
            src_paras.append((full, attrs, inner))
    if not src_paras:
        return draft

    draft_sa_matches: list[re.Match[str]] = []
    for m in _P_RE.finditer(draft):
        attrs, inner = m.group(1) or "", m.group(2) or ""
        classes = _tag_classes(attrs)
        is_sa = (
            ("sa" in classes or "shloka" in classes or bool(_deva_key(inner)))
            and "ru" not in classes
            and "tr" not in classes
            and "iast" not in classes
        )
        if is_sa:
            draft_sa_matches.append(m)

    used: set[int] = set()
    by_order = len(draft_sa_matches) == len(src_paras)

    def pick(inner: str, order_i: int) -> str | None:
        if by_order and 0 <= order_i < len(src_paras):
            used.add(order_i)
            return src_paras[order_i][2]
        v = _verse_key(inner)
        d = _deva_key(inner)
        multiline = _looks_like_multiline_sa(inner)
        if v:
            for i, (_f, _a, sinn) in enumerate(src_paras):
                if i in used:
                    continue
                if _verse_key(sinn) == v:
                    has_prev_pada = (
                        i > 0
                        and i - 1 not in used
                        and not _verse_key(src_paras[i - 1][2])
                        and bool(_deva_key(src_paras[i - 1][2]))
                    )
                    # Multi-pāda śloka in source: never leave only the last numbered line
                    # when the draft is a single block (with <br>) or was truncated to the end-pāda.
                    if multiline or has_prev_pada:
                        return _join_source_verse_block(src_paras, i, used)
                    used.add(i)
                    return sinn
        if d and len(d) >= 8:
            for i, (_f, _a, sinn) in enumerate(src_paras):
                if i in used:
                    continue
                sd = _deva_key(sinn)
                if not sd:
                    continue
                if d == sd or d[:16] == sd[:16] or d in sd or sd in d:
                    # Whole-śloka draft matched a single pāda — expand to verse block.
                    if multiline or (len(d) > len(sd) * 1.3 and _verse_key(inner)):
                        # find numbered line of this verse in source
                        vv = _verse_key(inner)
                        end = i
                        if vv:
                            for j, (__f, __a, sj) in enumerate(src_paras):
                                if _verse_key(sj) == vv:
                                    end = j
                                    break
                        return _join_source_verse_block(src_paras, end, used)
                    used.add(i)
                    return sinn
        return None

    pieces: list[str] = []
    last = 0
    for order_i, m in enumerate(draft_sa_matches):
        attrs, inner = m.group(1) or "", m.group(2) or ""
        new_inner = pick(inner, order_i)
        if new_inner is None and _sa_polluted(inner):
            for i, (_f, _a, sinn) in enumerate(src_paras):
                if i in used:
                    continue
                if _deva_key(sinn):
                    new_inner = sinn
                    used.add(i)
                    break
        if new_inner is None or new_inner == inner:
            # Truncated verse (only last pāda left after a bad restore): rebuild from source.
            v = _verse_key(inner)
            d = _deva_key(inner)
            if v and d:
                for i, (_f, _a, sinn) in enumerate(src_paras):
                    if i in used:
                        continue
                    if _verse_key(sinn) == v and (
                        d == _deva_key(sinn) or d in _deva_key(sinn) or _deva_key(sinn) in d
                    ):
                        # Draft equals only the last pāda — expand.
                        if len(_deva_key(sinn)) >= len(d) * 0.8:
                            block = _join_source_verse_block(src_paras, i, used)
                            if block != inner and _deva_key(block) != d:
                                new_inner = block
                        break
        if new_inner is None or new_inner == inner:
            continue
        pieces.append(draft[last : m.start()])
        pieces.append(f"<p{attrs}>{new_inner}</p>")
        last = m.end()
    if not pieces:
        return draft
    pieces.append(draft[last:])
    return "".join(pieces)


def _ru_lookup(html: str) -> dict[tuple[str, str], str]:
    """Map (kind, key) → full <p class=ru> from merged HTML (works inside div.shloka)."""
    index: dict[tuple[str, str], str] = {}
    pending: list[str] = []
    verse = ""
    for full, attrs, inner in _paragraphs(html):
        classes = _tag_classes(attrs)
        if "ru" in classes:
            d = "".join(_deva_key(x) for x in pending)
            v = verse or _verse_key(inner)
            if v:
                index[("v", v)] = full
            if d:
                index[("d", d)] = full
                if len(d) >= 12:
                    index.setdefault(("p", d[:20]), full)
            pending, verse = [], ""
            continue
        if "iast" in classes:
            continue
        chunk = inner
        d = _deva_key(chunk)
        if d:
            pending.append(chunk)
            verse = _verse_key(chunk) or verse
    return index


def apply_merged_pairs(draft_html: str, merged_html: str) -> tuple[str, int]:
    """Replace matching Russian <p> tags; never drop unmatched verses.

    Returns (html, n_replaced). If nothing matched, returns the original draft.
    """
    draft = draft_html or ""
    merged = merged_html or ""
    if not merged.strip() or not draft.strip():
        return draft, 0
    idx = _ru_lookup(merged)
    if not idx:
        return draft, 0

    replaced = 0
    pending: list[str] = []
    verse = ""
    pieces: list[str] = []
    last = 0
    for m in _P_RE.finditer(draft):
        attrs, inner = m.group(1) or "", m.group(2) or ""
        classes = _tag_classes(attrs)
        if "ru" in classes:
            d = "".join(_deva_key(x) for x in pending)
            v = verse or _verse_key(inner)
            new_ru = (idx.get(("v", v)) if v else None) or (idx.get(("d", d)) if d else None)
            if not new_ru and d and len(d) >= 12:
                new_ru = idx.get(("p", d[:20]))
            pending, verse = [], ""
            if new_ru and new_ru != m.group(0):
                pieces.append(draft[last : m.start()])
                pieces.append(new_ru)
                last = m.end()
                replaced += 1
            continue
        if "iast" in classes:
            continue
        if _deva_key(inner):
            pending.append(inner)
            verse = _verse_key(inner) or verse
    if replaced == 0:
        return draft, 0
    pieces.append(draft[last:])
    return "".join(pieces), replaced


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
    voice_block: str = "",
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

    system = MERGE_SYSTEM
    voice = (voice_block or "").strip()
    if voice:
        system = (
            system
            + "\n\n"
            + voice
            + "\n\nЕсли B молчит по термину — опирайся на лексикон голоса; "
            "живая вставка B важнее карточки при явном конфликте."
        )

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
        system=system,
    )
    merged = validate_translation_html(text, source_html=source)
    spliced, n = apply_merged_pairs(draft, merged)
    if n:
        return restore_sa_from_source(spliced, source), model, usage
    # A one-śloka model reply must never replace the rest of the leaf.
    if ru_count(merged) < ru_count(draft):
        raise ValueError(
            "Не удалось сопоставить шлоку с черновиком — страница не изменена. "
            "Вставьте перевод в поле под нужной шлокой ещё раз."
        )
    return restore_sa_from_source(merged, source), model, usage
