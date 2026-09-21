"""Apply IAST edits back onto Devanagari in draft + source HTML."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.services.iast_script import iast_to_deva, norm_iast

_P_RE = re.compile(r"<p(\s[^>]*)?>(.*?)</p>", re.I | re.S)
_CLASS_RE = re.compile(r"""\bclass=["']([^"']*)["']""", re.I)
_DEVA_RE = re.compile(r"[\u0900-\u097F]")
_IAST_MARK_RE = re.compile(r"[āīūṛṝḷḹṅñṭḍṇśṣḥṃṁĀĪŪṚṜḶḸṄÑṬḌṆŚṢḤṂṀ]")
_ENGLISH_RE = re.compile(
    r"\b(the|and|of|chapter|book|from|with|this|that|for|into|translated)\b",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class IastChange:
    old: str
    new: str


@dataclass
class IastCorrectResult:
    draft_html: str
    source_html: str
    changes: list[IastChange] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.changes)


def _classes(attrs: str | None) -> set[str]:
    if not attrs:
        return set()
    m = _CLASS_RE.search(attrs)
    if not m:
        return set()
    return {c.lower() for c in m.group(1).split()}


def _visible(inner: str) -> str:
    text = _TAG_RE.sub("", inner or "")
    return re.sub(r"\s+", " ", text).strip()


def _deva_only(text: str) -> str:
    return "".join(_DEVA_RE.findall(text or ""))


def _is_iast(attrs: str | None) -> bool:
    return "iast" in _classes(attrs)


def _looks_sanskrit_iast(text: str) -> bool:
    raw = _visible(text)
    if len(raw) < 2:
        return False
    if _ENGLISH_RE.search(raw) and not _IAST_MARK_RE.search(raw):
        return False
    if _IAST_MARK_RE.search(raw):
        return True
    letters = re.sub(r"[^A-Za-z]", "", raw)
    return len(letters) >= 3 and not _ENGLISH_RE.search(raw)


def _related(old_deva: str, new_deva: str) -> bool:
    o = _deva_only(old_deva)
    n = _deva_only(new_deva)
    if not o or not n:
        return False
    if o == n:
        return True
    ratio = SequenceMatcher(None, o, n).ratio()
    if ratio >= 0.55:
        return True
    prefix = min(8, len(o), len(n))
    return prefix >= 6 and o[:prefix] == n[:prefix]


def _set_inner(full: str, new_inner: str) -> str:
    return _P_RE.sub(lambda m: f"<p{m.group(1) or ''}>{new_inner}</p>", full, count=1)


def _replace_once(html: str, old: str, new: str) -> str:
    if not old or old == new or old not in html:
        return html
    return html.replace(old, new, 1)


def _paragraphs(html: str) -> list[tuple[str, str | None, str]]:
    out: list[tuple[str, str | None, str]] = []
    for m in _P_RE.finditer(html or ""):
        out.append((m.group(0), m.group(1), m.group(2)))
    return out


def _inject_iast_lines(html: str, lines: list[str] | None) -> str:
    if not lines:
        return html
    cleaned = [norm_iast(str(x)).strip() for x in lines]
    cleaned = [x for x in cleaned if x]
    if not cleaned:
        return html
    idx = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal idx
        attrs = m.group(1) or ""
        if _is_iast(attrs) and idx < len(cleaned):
            line = cleaned[idx]
            idx += 1
            return f"<p{attrs}>{line}</p>"
        return m.group(0)

    return _P_RE.sub(repl, html)


def apply_iast_corrections(
    draft_html: str,
    source_html: str,
    iast_lines: list[str] | None = None,
) -> IastCorrectResult:
    draft = _inject_iast_lines(draft_html or "", iast_lines)
    source = source_html or ""
    changes: list[IastChange] = []

    paras = _paragraphs(draft)
    last_deva: tuple[str, str] | None = None  # (full p, visible text)
    used_source: set[str] = set()

    def apply_pair(old_full: str, old_text: str, iast_text: str) -> None:
        nonlocal draft, source
        if not _looks_sanskrit_iast(iast_text):
            return
        new_deva = iast_to_deva(iast_text, old_deva=old_text).strip()
        if not _deva_only(new_deva):
            return
        if new_deva == old_text:
            return
        if not _related(old_text, new_deva):
            return
        new_full = _set_inner(old_full, new_deva)
        draft = _replace_once(draft, old_full, new_full)
        src_hit = None
        for full, _attrs, inner in _paragraphs(source):
            vis = _visible(inner)
            if vis == old_text and full not in used_source:
                src_hit = full
                break
        if src_hit:
            source = _replace_once(source, src_hit, _set_inner(src_hit, new_deva))
            used_source.add(_set_inner(src_hit, new_deva))
        changes.append(IastChange(old=old_text, new=new_deva))

    draft_has_deva = any(
        (not _is_iast(attrs)) and _deva_only(_visible(inner)) for _full, attrs, inner in paras
    )

    if draft_has_deva:
        for full, attrs, inner in paras:
            vis = _visible(inner)
            if _is_iast(attrs):
                if last_deva:
                    apply_pair(last_deva[0], last_deva[1], vis)
                    last_deva = None
                continue
            if _deva_only(vis):
                last_deva = (full, vis)
        return IastCorrectResult(draft_html=draft, source_html=source, changes=changes)

    src_deva = [
        (full, _visible(inner))
        for full, attrs, inner in _paragraphs(source)
        if not _is_iast(attrs) and _deva_only(_visible(inner))
    ]
    iast_ps = [(full, _visible(inner)) for full, attrs, inner in paras if _is_iast(attrs)]
    for (src_full, src_text), (_iast_full, iast_text) in zip(src_deva, iast_ps):
        apply_pair(src_full, src_text, iast_text)
        # apply_pair replaces in draft by old_full; for plain IAST draft the sa p is only in source.
        # Re-run source replace already happened; draft stays IAST-only.
    # apply_pair also tries to replace old_full in draft — source p is not in draft, replace is no-op.
    return IastCorrectResult(draft_html=draft, source_html=source, changes=changes)


def overlay_sa_onto_source(source_html: str, draft_html: str) -> tuple[str, list[IastChange]]:
    source = source_html or ""
    src_ps = [
        (full, _visible(inner))
        for full, attrs, inner in _paragraphs(source)
        if not _is_iast(attrs) and _deva_only(_visible(inner))
    ]
    draft_ps = [
        (full, _visible(inner))
        for full, attrs, inner in _paragraphs(draft_html or "")
        if not _is_iast(attrs) and _deva_only(_visible(inner))
    ]
    changes: list[IastChange] = []
    for (src_full, src_text), (_d_full, d_text) in zip(src_ps, draft_ps):
        if src_text == d_text:
            continue
        source = _replace_once(source, src_full, _set_inner(src_full, d_text))
        changes.append(IastChange(old=src_text, new=d_text))
    return source, changes


def source_for_digitize(
    *,
    saved_source: str,
    incoming_source: str | None,
    draft_html: str,
) -> tuple[str, list[IastChange]]:
    saved = saved_source or ""
    left = incoming_source if incoming_source is not None else saved
    overlay_from_saved, overlay_changes = overlay_sa_onto_source(saved, draft_html)
    left_changed = bool((incoming_source or "").strip()) and (left or "").strip() != saved.strip()
    if left_changed and not overlay_changes:
        return left, []
    if overlay_changes:
        return overlay_from_saved, overlay_changes
    return left, []
