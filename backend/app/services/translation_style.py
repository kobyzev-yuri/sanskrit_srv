"""Agreed Russian-translation templates (expert chooses before LLM)."""
from __future__ import annotations

from typing import Any

STYLE_INTERLINEAR = "interlinear"
STYLE_SAMASA = "samasa_gloss"
STYLE_CUSTOM = "custom"
STYLES = (STYLE_INTERLINEAR, STYLE_SAMASA, STYLE_CUSTOM)

ENGLISH_REPLACE = "replace"
ENGLISH_DROP = "drop"
ENGLISH_POLICIES = (ENGLISH_REPLACE, ENGLISH_DROP)

STYLES_CATALOG: list[dict[str, str]] = [
    {
        "id": STYLE_INTERLINEAR,
        "label": "Шлока + строка перевода",
        "hint": "После каждой санскритской строки — следующая строка литературным русским.",
    },
    {
        "id": STYLE_SAMASA,
        "label": "Самаса в скобках",
        "hint": "Сложение разбирается; члены санскрита в круглых скобках у русских слов, порядок — по смыслу.",
    },
    {
        "id": STYLE_CUSTOM,
        "label": "Свой шаблон",
        "hint": "Вёрстка и правила — только из заметок эксперта (согласовать заранее).",
    },
]


def default_translation_settings(
    *,
    style: str = STYLE_INTERLINEAR,
    english_comments: str = ENGLISH_REPLACE,
    notes: str = "",
) -> dict[str, Any]:
    st = style if style in STYLES else STYLE_INTERLINEAR
    en = english_comments if english_comments in ENGLISH_POLICIES else ENGLISH_REPLACE
    return {
        "style": st,
        "english_comments": en,
        "notes": (notes or "").strip()[:4000],
        "agreed": False,
        "agreed_by": None,
        "agreed_at": None,
    }


def project_task(project) -> str:
    settings = (getattr(project, "settings", None) or {}) if project is not None else {}
    task = str(settings.get("task") or "digitize").strip().lower()
    return "translate" if task == "translate" else "digitize"


def translation_cfg(project) -> dict[str, Any]:
    settings = (getattr(project, "settings", None) or {}) if project is not None else {}
    raw = settings.get("translation") or {}
    if not isinstance(raw, dict):
        raw = {}
    base = default_translation_settings()
    base.update({k: raw[k] for k in base if k in raw})
    return base


def translation_agreed(project) -> bool:
    return bool(translation_cfg(project).get("agreed"))


def _style_prompt(style: str) -> str:
    if style == STYLE_SAMASA:
        return """TEMPLATE samasa_gloss (mandatory):
- Keep each Sanskrit verse/prose block in Devanagari (class="sa shloka" or class="sa", lang="sa").
- After it, a literary Russian rendering (class="ru tr", lang="ru").
- Split compounds (samāsa) into members. In the Russian sentence, put the Sanskrit member in ASCII parentheses right after the Russian word it glosses.
- Russian word order is literary (members may be rearranged). Example:
  Sanskrit: यज्ञाश्वः
  Russian: жертвенный (यज्ञ) конь (अश्व)
- Do not leave a separate "glossary dump" instead of a readable Russian sentence.
- Numbered ślokas: keep the number on the Sanskrit line."""
    if style == STYLE_CUSTOM:
        return """TEMPLATE custom:
- Follow the expert notes below as the ONLY layout/style contract.
- Still keep Devanagari source lines visible; add Russian according to those notes.
- If notes are empty, fall back to: Sanskrit line, then Russian line."""
    return """TEMPLATE interlinear (mandatory):
- One printed Sanskrit line (śloka pāda / sūtra / mantra / heading) → one <p class="sa shloka" lang="sa"> (or class="sa" for prose).
- The IMMEDIATE next block is the Russian translation: <p class="ru tr" lang="ru">.
- Do not merge several pādas into one Russian paragraph unless the source is already one prose block.
- Keep verse numbers on the Sanskrit line.
- Headings: Sanskrit heading, then Russian heading (class="ru")."""


def _english_prompt(policy: str) -> str:
    if policy == ENGLISH_DROP:
        return """ENGLISH (and other Latin-script commentary printed in the source):
- Omit it entirely. Do not translate it. Do not keep the English text.
- If you add a Russian note of your own, it replaces the English — never bilingual leftovers."""
    return """ENGLISH (and other Latin-script commentary printed in the source):
- Replace with equivalent Russian (class="ru note" lang="ru").
- Do not leave English sentences in the output.
- If a Latin phrase is a conventional siglum (e.g. cf., viz.) you may drop it or render in Russian."""


def build_translate_prompt(
    *,
    source_html: str,
    cfg: dict[str, Any],
    current_html: str | None = None,
    directive: str | None = None,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
) -> str:
    style = str(cfg.get("style") or STYLE_INTERLINEAR)
    policy = str(cfg.get("english_comments") or ENGLISH_REPLACE)
    notes = (cfg.get("notes") or "").strip()
    parts = [
        "You produce a Russian translation HTML fragment of a Sanskrit page already restored as HTML.",
        "The SOURCE HTML is diplomatic text (Devanagari). Do NOT 'correct' Vedic/old spellings.",
        "Output ONLY an HTML fragment: <article class=\"page-style\" lang=\"ru\"> … </article>. No markdown, no preface.",
        "Keep Devanagari exactly as in the source. Russian is literary, not a word-for-word crib unless the template asks for glosses.",
        "Layout only via classes (sa, shloka, ru, tr, note, indent, centered, running-head, page-num). No inline style=, flex, float.",
        "FIGURES / IMAGES: copy every <img …> and <figure …> from the SOURCE HTML with the src= URL "
        "CHARACTER-FOR-CHARACTER identical (full /api/v1/pages/<uuid>/figures/crop-NN.png or emb-NN.png). "
        "Do not invent, shorten, or 'fix' UUIDs. Do not translate alt into a reason to change src. "
        "Do not use blob: URLs.",
        _style_prompt(style),
        _english_prompt(policy),
    ]
    if (
        chunk_index is not None
        and chunk_total is not None
        and chunk_total > 1
        and chunk_index >= 1
    ):
        parts.append(
            f"CHUNK {chunk_index} of {chunk_total} of ONE printed page. "
            "Translate ONLY this SOURCE fragment. Do not invent content from other chunks. "
            "Output one <article>…</article> covering just this part; parts will be concatenated."
        )
    if notes:
        parts.append("EXPERT NOTES (binding):\n" + notes[:4000])
    if (directive or "").strip():
        parts.append("ADDITIONAL DIRECTIVE for this page:\n" + directive.strip()[:4000])
    if (current_html or "").strip():
        parts.append(
            "PREVIOUS TRANSLATION DRAFT (revise it; do not start from scratch unless the directive says so):\n"
            + current_html.strip()[:20000]
        )
    # Per-chunk source is already sized; keep a hard ceiling for single-shot pages.
    parts.append("SOURCE HTML:\n" + (source_html or "").strip()[:40000])
    return "\n\n".join(parts)
