"""Professor / translator voice memory: style card + lexical few-shot selection."""
from __future__ import annotations

import re
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Voice, VoiceExample

_DEVA_RE = re.compile(r"[\u0900-\u097F]+")
_VERSE_RE = re.compile(r"॥\s*([०-९0-9]+)\s*॥")
_DIGIT = str.maketrans("०१२३४५६७८९", "0123456789")
# Russian word (iast) — allow spaces/hyphens inside IAST
_GLOSS_RE = re.compile(
    r"([А-Яа-яЁёA-Za-z\-]+)\s*\(([a-zA-ZāīūṛṝḷḹṅñṭḍṇśṣḥṃṁĀĪŪṚṜḶḸṄÑṬḌṆŚṢḤṂṀ][a-zA-ZāīūṛṝḷḹṅñṭḍṇśṣḥṃṁĀĪŪṚṜḶḸṄÑṬḌṆŚṢḤṂṀ\-\s]*)\)"
)
_IAST_TOKEN_RE = re.compile(
    r"[a-zA-ZāīūṛṝḷḹṅñṭḍṇśṣḥṃṁĀĪŪṚṜḶḸṄÑṬḌṆŚṢḤṂṀ]+(?:-[a-zA-ZāīūṛṝḷḹṅñṭḍṇśṣḥṃṁĀĪŪṚṜḶḸṄÑṬḌṆŚṢḤṂṀ]+)*"
)

VOICE_BLOCK_MAX = 8_000
EXAMPLE_MAX_CHARS = 1_800
DEFAULT_TOP_K = 3
DEFAULT_DOMAIN = "kashmir_shaivism"

DEFAULT_GRAMMAR = [
    "оптатив / императив 3 л. (saṃcarvyatām) → «да …», не изъявительное «вкушается»",
    "сложения по членам: jīvat-śivatvam → «состояние Шивы при жизни», не «живой Шива»",
]
DEFAULT_PHRASE = [
    "читаемое русское предложение в формате шаблона; без «буквально…» и таблиц разбора",
]
DEFAULT_PREFER = "grammar_and_terms_over_fluency"


def empty_style_card(*, domain: str = DEFAULT_DOMAIN) -> dict[str, Any]:
    return {
        "domain": domain,
        "lexicon": {},
        "grammar": list(DEFAULT_GRAMMAR),
        "phrase": list(DEFAULT_PHRASE),
        "prefer": DEFAULT_PREFER,
    }


def dewa_key(text: str) -> str:
    return "".join(_DEVA_RE.findall(text or ""))


def verse_key(text: str) -> str | None:
    m = _VERSE_RE.search(text or "")
    if not m:
        return None
    return m.group(1).translate(_DIGIT)


def iast_tokens(text: str) -> set[str]:
    found: set[str] = set()
    for m in _GLOSS_RE.finditer(text or ""):
        tok = m.group(2).strip().lower().replace(" ", "")
        if tok:
            found.add(tok)
            for part in tok.split("-"):
                if len(part) >= 3:
                    found.add(part)
    for m in _IAST_TOKEN_RE.finditer(text or ""):
        tok = m.group(0).lower()
        if len(tok) >= 3 and any(c in tok for c in "āīūṛṃḥśṣṭḍṇñṅ"):
            found.add(tok)
    return found


def gloss_pairs(ru: str) -> list[tuple[str, str]]:
    """(russian_word, iast) from iast_gloss line."""
    out: list[tuple[str, str]] = []
    for m in _GLOSS_RE.finditer(ru or ""):
        ru_w = m.group(1).strip()
        iast = m.group(2).strip().lower().replace(" ", "")
        if ru_w and iast:
            out.append((ru_w, iast))
    return out


def _char_ngrams(s: str, n: int = 3) -> set[str]:
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def score_example(
    example: VoiceExample,
    *,
    query_deva: str,
    query_iast: set[str],
) -> float:
    ex_deva = dewa_key(example.source_sa)
    dewa_score = _jaccard(_char_ngrams(query_deva), _char_ngrams(ex_deva)) if query_deva else 0.0
    if query_deva and ex_deva and (query_deva in ex_deva or ex_deva in query_deva):
        dewa_score = max(dewa_score, 0.85)
    ex_iast = iast_tokens(example.target_ru) | iast_tokens(example.source_sa)
    iast_score = _jaccard(query_iast, ex_iast) if query_iast else 0.0
    return 0.55 * dewa_score + 0.45 * iast_score


def select_examples(
    examples: list[VoiceExample],
    *,
    source_html: str,
    draft_html: str | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> list[VoiceExample]:
    if not examples:
        return []
    query_deva = dewa_key(source_html)
    query_iast = iast_tokens(draft_html or "") | iast_tokens(source_html)
    ranked = sorted(
        examples,
        key=lambda ex: score_example(ex, query_deva=query_deva, query_iast=query_iast),
        reverse=True,
    )
    picked: list[VoiceExample] = []
    for ex in ranked:
        if score_example(ex, query_deva=query_deva, query_iast=query_iast) <= 0 and picked:
            break
        picked.append(ex)
        if len(picked) >= top_k:
            break
    if not picked and ranked:
        # Always give 1–2 anchors so an empty bank never silently drops voice.
        picked = ranked[: min(2, len(ranked))]
    return picked


def format_style_card(card: dict[str, Any] | None, *, display_name: str) -> str:
    c = card if isinstance(card, dict) else {}
    lines = [f"ГОЛОС ПЕРЕВОДЧИКА «{display_name}» (мягкая настройка, формат шаблона не ломать):"]
    domain = (c.get("domain") or "").strip()
    if domain:
        lines.append(f"Домен: {domain}. Не переноси термины чужого домена без оснований в корне.")
    prefer = (c.get("prefer") or DEFAULT_PREFER).strip()
    lines.append(f"Предпочтение: {prefer}.")
    lex = c.get("lexicon") if isinstance(c.get("lexicon"), dict) else {}
    if lex:
        lines.append("Лексикон (IAST → предпочитаемый русский):")
        for iast, ru in list(lex.items())[:80]:
            lines.append(f"  - {iast}: {ru}")
    grammar = c.get("grammar") if isinstance(c.get("grammar"), list) else []
    if grammar:
        lines.append("Грамматика / разбор сложений:")
        for g in grammar[:20]:
            lines.append(f"  - {g}")
    phrase = c.get("phrase") if isinstance(c.get("phrase"), list) else []
    if phrase:
        lines.append("Фраза:")
        for p in phrase[:12]:
            lines.append(f"  - {p}")
    return "\n".join(lines)


def format_examples_block(examples: list[VoiceExample]) -> str:
    if not examples:
        return ""
    parts = [
        "ЭТАЛОНЫ ГОЛОСА (few-shot; копируй стиль, не содержание чужой шлоки):"
    ]
    for i, ex in enumerate(examples, start=1):
        sa = (ex.source_sa or "").strip()[:EXAMPLE_MAX_CHARS]
        ru = (ex.target_ru or "").strip()[:EXAMPLE_MAX_CHARS]
        vk = ex.verse_key or verse_key(sa) or verse_key(ru) or "?"
        parts.append(f"--- эталон {i} (॥ {vk} ॥) ---\nSA:\n{sa}\nRU:\n{ru}")
    return "\n".join(parts)


def build_voice_block(
    voice: Voice,
    examples: list[VoiceExample],
) -> str:
    chunks = [
        format_style_card(voice.style_card, display_name=voice.display_name),
        format_examples_block(examples),
    ]
    text = "\n\n".join(c for c in chunks if c).strip()
    return text[:VOICE_BLOCK_MAX]


def attach_voice_to_cfg(
    db: Session,
    cfg: dict[str, Any],
    *,
    source_html: str,
    draft_html: str | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Copy cfg and inject voice_block from project voice_id (if any)."""
    out = dict(cfg)
    out.pop("voice_block", None)
    raw_id = (cfg.get("voice_id") or "").strip()
    if not raw_id:
        return out
    try:
        vid = uuid.UUID(raw_id)
    except ValueError:
        return out
    voice = db.get(Voice, vid)
    if voice is None:
        return out
    examples = list(
        db.scalars(
            select(VoiceExample)
            .where(VoiceExample.voice_id == voice.id)
            .order_by(VoiceExample.created_at.desc())
        ).all()
    )
    picked = select_examples(
        examples, source_html=source_html, draft_html=draft_html, top_k=top_k
    )
    block = build_voice_block(voice, picked)
    if block:
        out["voice_block"] = block
        out["voice_name"] = voice.display_name
        out["voice_domain"] = voice.domain
    return out


def distill_style_card(
    examples: list[VoiceExample],
    *,
    domain: str = DEFAULT_DOMAIN,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Heuristic card from gloss pairs — no LLM. Merges into existing lexicon."""
    base = empty_style_card(domain=domain)
    if isinstance(existing, dict):
        if isinstance(existing.get("lexicon"), dict):
            base["lexicon"] = dict(existing["lexicon"])
        if isinstance(existing.get("grammar"), list) and existing["grammar"]:
            base["grammar"] = list(existing["grammar"])
        if isinstance(existing.get("phrase"), list) and existing["phrase"]:
            base["phrase"] = list(existing["phrase"])
        if existing.get("prefer"):
            base["prefer"] = str(existing["prefer"])
        if existing.get("domain"):
            base["domain"] = str(existing["domain"])
    base["domain"] = domain or base["domain"]

    # iast -> Counter of russian forms
    tallies: dict[str, Counter[str]] = {}
    for ex in examples:
        for ru_w, iast in gloss_pairs(ex.target_ru):
            key = iast.strip().lower()
            if len(key) < 2:
                continue
            tallies.setdefault(key, Counter())[ru_w] += 1
            # also stem before hyphen for compound members
            if "-" in key:
                for part in key.split("-"):
                    if len(part) >= 4:
                        tallies.setdefault(part, Counter())[ru_w] += 1

    lex = dict(base["lexicon"])
    for iast, counter in tallies.items():
        ru, n = counter.most_common(1)[0]
        if n >= 1:
            lex[iast] = ru
    # Prefer shorter dictionary keys that appear often
    base["lexicon"] = dict(sorted(lex.items(), key=lambda kv: (-len(kv[0]), kv[0]))[:120])
    return base


def parse_batch_examples(text: str) -> list[tuple[str, str]]:
    """Parse pasted bank: blocks separated by === or SA:/RU: pairs."""
    raw = (text or "").strip()
    if not raw:
        return []
    blocks = re.split(r"\n\s*===\s*\n", raw)
    pairs: list[tuple[str, str]] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        sa_m = re.search(r"(?is)^\s*SA\s*:\s*(.*?)\s*^RU\s*:\s*(.*)\s*$", block)
        if sa_m:
            sa, ru = sa_m.group(1).strip(), sa_m.group(2).strip()
            if sa and ru:
                pairs.append((sa, ru))
            continue
        # HTML-ish: first dewanagari paragraph + russian
        paras = re.findall(r"<p[^>]*>(.*?)</p>", block, flags=re.I | re.S)
        if len(paras) >= 2:
            plain = [re.sub(r"<[^>]+>", "", p).strip() for p in paras]
            sa_parts = [p for p in plain if _DEVA_RE.search(p)]
            ru_parts = [p for p in plain if not _DEVA_RE.search(p) and p]
            if sa_parts and ru_parts:
                pairs.append((sa_parts[0], ru_parts[0]))
                continue
        # Plain: lines with dewanagari, then Russian
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        sa_lines = [ln for ln in lines if _DEVA_RE.search(ln)]
        ru_lines = [ln for ln in lines if not _DEVA_RE.search(ln)]
        if sa_lines and ru_lines:
            pairs.append(("\n".join(sa_lines), "\n".join(ru_lines)))
    return pairs


def slugify_voice(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9а-яё]+", "-", s, flags=re.I)
    s = re.sub(r"-+", "-", s).strip("-")
    # ASCII-ish slug for DB uniqueness
    s = re.sub(r"[^a-z0-9\-]+", "", s) or "voice"
    return s[:120]
