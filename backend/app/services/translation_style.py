"""Agreed Russian-translation templates (expert chooses before LLM)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

STYLE_INTERLINEAR = "interlinear"
STYLE_IAST_GLOSS = "iast_gloss"
STYLE_SAMASA = "samasa_gloss"
STYLE_CUSTOM = "custom"
STYLES = (STYLE_INTERLINEAR, STYLE_IAST_GLOSS, STYLE_SAMASA, STYLE_CUSTOM)

ENGLISH_REPLACE = "replace"
ENGLISH_DROP = "drop"
ENGLISH_POLICIES = (ENGLISH_REPLACE, ENGLISH_DROP)
NOTES_MAX = 20_000

STYLES_CATALOG: list[dict[str, str]] = [
    {
        "id": STYLE_INTERLINEAR,
        "label": "Шлока + строка перевода",
        "hint": "После каждой санскритской строки — следующая строка литературным русским.",
    },
    {
        "id": STYLE_IAST_GLOSS,
        "label": "Пословно: рус. (IAST)",
        "hint": "Грамотное русское предложение: слово (IAST); добавленное для связки — в [скобках]. Словарь кашмирского шиваизма в системном промпте.",
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
        "notes": (notes or "").strip()[:NOTES_MAX],
        "agreed": True,
        "agreed_by": None,
        "agreed_at": None,
    }


TASK_DIGITIZE = "digitize"
TASK_TRANSLATE = "translate"
TASK_TRANSLITERATE = "transliterate"
SOURCE_HTML_TASKS = (TASK_TRANSLATE, TASK_TRANSLITERATE)

STYLE_IAST_PLAIN = "iast_plain"
STYLE_IAST_BLOCK = "iast_block"
TRANSLIT_STYLES = (STYLE_IAST_PLAIN, STYLE_IAST_BLOCK)
ENGLISH_KEEP = "keep"
TRANSLIT_ENGLISH_POLICIES = (ENGLISH_KEEP, ENGLISH_DROP)

TRANSLIT_STYLES_CATALOG: list[dict[str, str]] = [
    {
        "id": STYLE_IAST_PLAIN,
        "label": "Только IAST",
        "hint": "Страница целиком латиницей IAST (без деванагари в черновике).",
    },
    {
        "id": STYLE_IAST_BLOCK,
        "label": "Шлока + блок IAST",
        "hint": "После каждого санскритского блока — тот же текст транслитерацией IAST.",
    },
]


def project_task(project) -> str:
    settings = (getattr(project, "settings", None) or {}) if project is not None else {}
    task = str(settings.get("task") or "digitize").strip().lower()
    if task in SOURCE_HTML_TASKS:
        return task
    return TASK_DIGITIZE


def is_source_html_task(project) -> bool:
    return project_task(project) in SOURCE_HTML_TASKS


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


def persist_translation_cfg(project, cfg: dict[str, Any]) -> None:
    settings = dict(getattr(project, "settings", None) or {})
    settings["translation"] = cfg
    project.settings = settings


def lock_translation_template(project, user: Any = None) -> dict[str, Any]:
    """Creating or starting a translation locks the chosen template so LLM can run."""
    cfg = translation_cfg(project)
    if cfg.get("agreed"):
        return cfg
    cfg["agreed"] = True
    uid = getattr(user, "id", None)
    cfg["agreed_by"] = str(uid) if uid else None
    cfg["agreed_at"] = datetime.now(timezone.utc).isoformat()
    persist_translation_cfg(project, cfg)
    return cfg


def _read_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def _style_prompt(style: str) -> str:
    if style == STYLE_IAST_GLOSS:
        return _read_prompt("iast_gloss_system.txt")
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


def _engine_system(*, style: str, policy: str, notes: str) -> str:
    if style == STYLE_IAST_GLOSS:
        parts = [_read_prompt("iast_gloss_system.txt"), _english_prompt(policy)]
        if notes:
            parts.append("ДОПОЛНИТЕЛЬНЫЙ СЛОВАРЬ / ЗАМЕТКИ ЭКСПЕРТА:\n" + notes[:NOTES_MAX])
        return "\n\n".join(parts)
    parts = [
        "You produce a Russian translation HTML fragment of a Sanskrit page already restored as HTML.",
        "Start the reply with <article. Output ONLY the HTML fragment — no analysis, plans, or English commentary.",
        "Output ONLY an HTML fragment: <article class=\"page-style\" lang=\"ru\"> … </article>. No markdown, no preface.",
        "Keep Devanagari exactly as in the source. How Russian relates to Sanskrit is defined ONLY by the TEMPLATE (literary vs word-gloss). Do not mix templates.",
        "Layout only via classes (sa, shloka, ru, tr, note, indent, centered, running-head, page-num). No inline style=, flex, float.",
        "FIGURES / IMAGES: copy every <img …> and <figure …> from the SOURCE HTML with the src= URL "
        "CHARACTER-FOR-CHARACTER identical (full /api/v1/pages/<uuid>/figures/crop-NN.png or emb-NN.png). "
        "Do not invent, shorten, or 'fix' UUIDs. Do not translate alt into a reason to change src. "
        "Do not use blob: URLs.",
        _style_prompt(style),
        _english_prompt(policy),
    ]
    if notes:
        parts.append("EXPERT NOTES (binding):\n" + notes[:NOTES_MAX])
    return "\n\n".join(parts)


def _user_extras(
    *,
    directive: str | None,
    current_html: str | None,
    chunk_index: int | None,
    chunk_total: int | None,
) -> list[str]:
    extra: list[str] = []
    if (
        chunk_index is not None
        and chunk_total is not None
        and chunk_total > 1
        and chunk_index >= 1
    ):
        extra.append(
            f"CHUNK {chunk_index} of {chunk_total} of ONE printed page. "
            "Translate ONLY this SOURCE fragment. Do not invent content from other chunks. "
            "Output one <article>…</article> covering just this part; parts will be concatenated."
        )
    if (directive or "").strip():
        extra.append("ADDITIONAL DIRECTIVE for this page:\n" + directive.strip()[:NOTES_MAX])
    if (current_html or "").strip():
        extra.append(
            "PREVIOUS TRANSLATION DRAFT (revise it; do not start from scratch unless the directive says so):\n"
            + current_html.strip()[:20000]
        )
    return extra


def build_translate_messages(
    *,
    source_html: str,
    cfg: dict[str, Any],
    current_html: str | None = None,
    directive: str | None = None,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
) -> tuple[str, str]:
    """Return (system, user). Page text never goes into system."""
    style = str(cfg.get("style") or STYLE_INTERLINEAR)
    policy = str(cfg.get("english_comments") or ENGLISH_REPLACE)
    notes = (cfg.get("notes") or "").strip()
    system = _engine_system(style=style, policy=policy, notes=notes)
    extras = _user_extras(
        directive=directive,
        current_html=current_html,
        chunk_index=chunk_index,
        chunk_total=chunk_total,
    )
    source = (source_html or "").strip()[:40000]
    if style == STYLE_IAST_GLOSS:
        user = _read_prompt("iast_gloss_user.txt").replace("{source}", source, 1)
        if extras:
            user = user + "\n\n" + "\n\n".join(extras)
        return system, user
    user_parts = [
        "The SOURCE HTML is diplomatic text (Devanagari). Do NOT 'correct' Vedic/old spellings unless the template says otherwise.",
        *extras,
        "SOURCE HTML:\n" + source,
    ]
    return system, "\n\n".join(user_parts)


def build_translate_prompt(
    *,
    source_html: str,
    cfg: dict[str, Any],
    current_html: str | None = None,
    directive: str | None = None,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
) -> str:
    system, user = build_translate_messages(
        source_html=source_html,
        cfg=cfg,
        current_html=current_html,
        directive=directive,
        chunk_index=chunk_index,
        chunk_total=chunk_total,
    )
    return system + "\n\n" + user


def build_translate_batch_messages(
    *,
    pages: list[tuple[int, str]],
    cfg: dict[str, Any],
) -> tuple[str, str]:
    style = str(cfg.get("style") or STYLE_INTERLINEAR)
    policy = str(cfg.get("english_comments") or ENGLISH_REPLACE)
    notes = (cfg.get("notes") or "").strip()
    nos = [int(n) for n, _ in pages]
    first, last = nos[0], nos[-1]
    system = _engine_system(style=style, policy=policy, notes=notes)
    user_parts = [
        f"You are given {len(pages)} consecutive pages {first}–{last}. "
        "Use neighbors for verse continuation and consistent terminology. "
        "Do not copy body text from one page onto another.",
        f"You MUST emit a block for EVERY page {first}–{last} — do not stop after the first. "
        "A short or blank leaf still gets its own block.",
        "Output ONLY labeled HTML. For every page emit exactly these two lines of structure:",
        "===PAGE N===",
        '<article class="page-style" lang="ru">…</article>',
        "No commentary, markdown fences, or extra headings outside those blocks.",
    ]
    for no, src in pages:
        user_parts.append(f"SOURCE HTML for page {no}:\n" + (src or "").strip()[:40000])
    return system, "\n\n".join(user_parts)


def build_translate_batch_prompt(
    *,
    pages: list[tuple[int, str]],
    cfg: dict[str, Any],
) -> str:
    """One prompt for several consecutive source pages (===PAGE n=== output)."""
    system, user = build_translate_batch_messages(pages=pages, cfg=cfg)
    return system + "\n\n" + user


def default_transliteration_settings(
    *,
    style: str = STYLE_IAST_BLOCK,
    english_comments: str = ENGLISH_DROP,
    notes: str = "",
) -> dict[str, Any]:
    st = style if style in TRANSLIT_STYLES else STYLE_IAST_BLOCK
    en = english_comments if english_comments in TRANSLIT_ENGLISH_POLICIES else ENGLISH_DROP
    return {
        "style": st,
        "english_comments": en,
        "notes": (notes or "").strip()[:NOTES_MAX],
        "agreed": True,
        "agreed_by": None,
        "agreed_at": None,
    }


def transliteration_cfg(project) -> dict[str, Any]:
    settings = (getattr(project, "settings", None) or {}) if project is not None else {}
    raw = settings.get("transliteration") or {}
    if not isinstance(raw, dict):
        raw = {}
    base = default_transliteration_settings()
    base.update({k: raw[k] for k in base if k in raw})
    return base


def transliteration_agreed(project) -> bool:
    return bool(transliteration_cfg(project).get("agreed"))


def persist_transliteration_cfg(project, cfg: dict[str, Any]) -> None:
    settings = dict(getattr(project, "settings", None) or {})
    settings["transliteration"] = cfg
    project.settings = settings


def lock_transliteration_template(project, user: Any = None) -> dict[str, Any]:
    cfg = transliteration_cfg(project)
    if cfg.get("agreed"):
        return cfg
    cfg["agreed"] = True
    uid = getattr(user, "id", None)
    cfg["agreed_by"] = str(uid) if uid else None
    cfg["agreed_at"] = datetime.now(timezone.utc).isoformat()
    persist_transliteration_cfg(project, cfg)
    return cfg


def _translit_style_prompt(style: str) -> str:
    if style == STYLE_IAST_PLAIN:
        return """TEMPLATE iast_plain (mandatory):
- Output IAST only. Do NOT keep Devanagari body text in the draft.
- One source block → one <p class="iast" lang="sa-Latn"> (or h1/h2/li with the same class/lang).
- Scholarly IAST: ā ī ū ṛ ṝ ḷ ḹ ṅ ñ ṭ ḍ ṇ ś ṣ ḥ ṃ; anusvāra ṃ; visarga ḥ; avagraha '.
- Do not invent sandhi splits or 'correct' Vedic/old spellings unless expert notes say so.
- Headings, running heads, page numbers, figures: keep structure; transliterate Sanskrit captions.
- Wrapper: <article class="page-style" lang="sa-Latn">."""
    return """TEMPLATE iast_block (mandatory):
- Keep each Sanskrit verse/prose block in Devanagari exactly as in the source
  (<p class="sa shloka" lang="sa"> or class="sa" for prose; headings keep class="sa").
- The IMMEDIATE next block is the IAST transliteration of THAT same line:
  <p class="iast" lang="sa-Latn">.
- Do not merge several pādas into one IAST paragraph unless the source is already one prose block.
- Keep verse numbers on the Devanagari line; repeat them on the IAST line if they are part of the printed line.
- Wrapper: <article class="page-style" lang="sa">."""


def _translit_english_prompt(policy: str) -> str:
    if policy == ENGLISH_KEEP:
        return """ENGLISH (and other non-Sanskrit Latin commentary in the source):
- Keep it as <p class="note" lang="en"> (or the original block tag). Do not convert English into IAST.
- Sanskrit in the same block still becomes IAST (plain) or Devanagari+IAST (block)."""
    return """ENGLISH (and other non-Sanskrit Latin commentary in the source):
- Omit it entirely. Do not transliterate English as if it were Sanskrit."""


def _translit_engine_system(*, style: str, policy: str, notes: str) -> str:
    parts = [
        "You produce an IAST transliteration HTML fragment of a Sanskrit page already restored as HTML.",
        "Start the reply with <article. Output ONLY the HTML fragment — no analysis, plans, or commentary.",
        "Output ONLY an HTML fragment: <article class=\"page-style\" …> … </article>. No markdown, no preface.",
        "How IAST relates to Devanagari is defined ONLY by the TEMPLATE. Do not mix templates.",
        "Layout only via classes (sa, shloka, iast, note, indent, centered, running-head, page-num). No inline style=, flex, float.",
        "FIGURES / IMAGES: copy every <img …> and <figure …> from the SOURCE HTML with the src= URL "
        "CHARACTER-FOR-CHARACTER identical (full /api/v1/pages/<uuid>/figures/crop-NN.png or emb-NN.png). "
        "Do not invent, shorten, or 'fix' UUIDs. Do not use blob: URLs.",
        _translit_style_prompt(style),
        _translit_english_prompt(policy),
    ]
    if notes:
        parts.append("EXPERT NOTES (binding):\n" + notes[:NOTES_MAX])
    return "\n\n".join(parts)


def _translit_user_extras(
    *,
    directive: str | None,
    current_html: str | None,
    chunk_index: int | None,
    chunk_total: int | None,
) -> list[str]:
    extra: list[str] = []
    if (
        chunk_index is not None
        and chunk_total is not None
        and chunk_total > 1
        and chunk_index >= 1
    ):
        extra.append(
            f"CHUNK {chunk_index} of {chunk_total} of ONE printed page. "
            "Transliterate ONLY this SOURCE fragment. Do not invent content from other chunks. "
            "Output one <article>…</article> covering just this part; parts will be concatenated."
        )
    if (directive or "").strip():
        extra.append("ADDITIONAL DIRECTIVE for this page:\n" + directive.strip()[:NOTES_MAX])
    if (current_html or "").strip():
        extra.append(
            "PREVIOUS IAST DRAFT (revise it; do not start from scratch unless the directive says so):\n"
            + current_html.strip()[:20000]
        )
    return extra


def build_transliterate_messages(
    *,
    source_html: str,
    cfg: dict[str, Any],
    current_html: str | None = None,
    directive: str | None = None,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
) -> tuple[str, str]:
    style = str(cfg.get("style") or STYLE_IAST_BLOCK)
    policy = str(cfg.get("english_comments") or ENGLISH_DROP)
    notes = (cfg.get("notes") or "").strip()
    system = _translit_engine_system(style=style, policy=policy, notes=notes)
    extras = _translit_user_extras(
        directive=directive,
        current_html=current_html,
        chunk_index=chunk_index,
        chunk_total=chunk_total,
    )
    source = (source_html or "").strip()[:40000]
    user_parts = [
        "The SOURCE HTML is diplomatic Devanagari. Transliterate into scholarly IAST. "
        "Do NOT 'correct' Vedic/old spellings unless the template or notes say otherwise.",
        *extras,
        "SOURCE HTML:\n" + source,
    ]
    return system, "\n\n".join(user_parts)


def build_transliterate_batch_messages(
    *,
    pages: list[tuple[int, str]],
    cfg: dict[str, Any],
) -> tuple[str, str]:
    style = str(cfg.get("style") or STYLE_IAST_BLOCK)
    policy = str(cfg.get("english_comments") or ENGLISH_DROP)
    notes = (cfg.get("notes") or "").strip()
    nos = [int(n) for n, _ in pages]
    first, last = nos[0], nos[-1]
    system = _translit_engine_system(style=style, policy=policy, notes=notes)
    user_parts = [
        f"You are given {len(pages)} consecutive pages {first}–{last}. "
        "Use neighbors for verse continuation. Do not copy body text from one page onto another.",
        f"You MUST emit a block for EVERY page {first}–{last} — do not stop after the first. "
        "A short or blank leaf still gets its own block.",
        "Output ONLY labeled HTML. For every page emit exactly these two lines of structure:",
        "===PAGE N===",
        '<article class="page-style" lang="sa-Latn">…</article>'
        if style == STYLE_IAST_PLAIN
        else '<article class="page-style" lang="sa">…</article>',
        "No commentary, markdown fences, or extra headings outside those blocks.",
    ]
    for no, src in pages:
        user_parts.append(f"SOURCE HTML for page {no}:\n" + (src or "").strip()[:40000])
    return system, "\n\n".join(user_parts)
