"""Scholarly IAST ↔ Devanagari (NFC). Prefer indic_transliteration when installed."""
from __future__ import annotations

import unicodedata

try:
    from indic_transliteration import sanscript as _sanscript
except ImportError:  # pragma: no cover - optional at runtime
    _sanscript = None


_VIRAMA = "्"
_ANUSVARA = "ं"
_VISARGA = "ः"
_AVAGRAHA = "ऽ"
_CANDRA = "ँ"
_VEDIC_M = "ꣳ"

_CONS = [
    ("kṣ", "क्ष"),
    ("jñ", "ज्ञ"),
    ("kh", "ख"),
    ("gh", "घ"),
    ("ch", "छ"),
    ("jh", "झ"),
    ("ṭh", "ठ"),
    ("ḍh", "ढ"),
    ("th", "थ"),
    ("dh", "ध"),
    ("ph", "फ"),
    ("bh", "भ"),
    ("ṅ", "ङ"),
    ("ñ", "ञ"),
    ("ṇ", "ण"),
    ("ṭ", "ट"),
    ("ḍ", "ड"),
    ("ś", "श"),
    ("ṣ", "ष"),
    ("ḻ", "ळ"),
    ("k", "क"),
    ("g", "ग"),
    ("c", "च"),
    ("j", "ज"),
    ("t", "त"),
    ("d", "द"),
    ("n", "न"),
    ("p", "प"),
    ("b", "ब"),
    ("m", "म"),
    ("y", "य"),
    ("r", "र"),
    ("l", "ल"),
    ("v", "व"),
    ("s", "स"),
    ("h", "ह"),
]
_CONS_IAST = [a for a, _ in _CONS]
_IAST_TO_CONS = {a: b for a, b in _CONS}
_CONS_TO_IAST = {b: a for a, b in _CONS if len(b) == 1}

_INDEP = {
    "a": "अ",
    "ā": "आ",
    "i": "इ",
    "ī": "ई",
    "u": "उ",
    "ū": "ऊ",
    "ṛ": "ऋ",
    "ṝ": "ॠ",
    "ḷ": "ऌ",
    "ḹ": "ॡ",
    "e": "ए",
    "ai": "ऐ",
    "o": "ओ",
    "au": "औ",
}
_MATRA = {
    "a": "",
    "ā": "ा",
    "i": "ि",
    "ī": "ी",
    "u": "ु",
    "ū": "ू",
    "ṛ": "ृ",
    "ṝ": "ॄ",
    "ḷ": "ॢ",
    "ḹ": "ॣ",
    "e": "े",
    "ai": "ै",
    "o": "ो",
    "au": "ौ",
}
_VOWEL_IAST = ("ai", "au", "ā", "ī", "ū", "ṛ", "ṝ", "ḷ", "ḹ", "e", "o", "a", "i", "u")
_MATRA_TO_IAST = {v: k for k, v in _MATRA.items() if v}
_INDEP_TO_IAST = {v: k for k, v in _INDEP.items()}
_DEV_CONS = frozenset(_CONS_TO_IAST) | set("कखगघङचछजझञटठडढणतथदधनपफबभमयरलळवशषसह")
_DEV_DIGITS = "०१२३४५६७८९"
_ASCII_TO_DEV_DIGIT = {str(i): d for i, d in enumerate(_DEV_DIGITS)}
_DEV_TO_ASCII_DIGIT = {d: str(i) for i, d in enumerate(_DEV_DIGITS)}


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text or "")


def norm_iast(text: str) -> str:
    s = nfc(text).replace("ṁ", "ṃ").replace("Ṁ", "Ṃ")
    s = s.replace("||", "॥").replace("|", "।")
    s = " ".join(s.split())
    return s.strip()


def _match(rest: str, tokens: tuple[str, ...] | list[str]) -> str | None:
    low = rest.lower()
    for tok in tokens:
        if low.startswith(tok):
            return tok
    return None


def _iast_to_deva_builtin(text: str) -> str:
    s = nfc(text).replace("ṁ", "ṃ").replace("Ṁ", "Ṃ")
    out: list[str] = []
    pending: str | None = None
    i = 0
    n = len(s)

    def flush(*, dead: bool) -> None:
        nonlocal pending
        if pending:
            out.append(pending + (_VIRAMA if dead else ""))
            pending = None

    while i < n:
        ch = s[i]
        rest = s[i:]
        if rest.startswith("||"):
            flush(dead=True)
            out.append("॥")
            i += 2
            continue
        if ch == "|":
            flush(dead=True)
            out.append("।")
            i += 1
            continue
        if rest.lower().startswith("oṃ") and pending is None:
            out.append("ॐ")
            i += 2
            continue
        cons = _match(rest, _CONS_IAST)
        if cons:
            letter = _IAST_TO_CONS[cons]
            if pending:
                pending = pending + _VIRAMA + letter
            else:
                pending = letter
            i += len(cons)
            continue
        vow = _match(rest, _VOWEL_IAST)
        if vow:
            if pending:
                out.append(pending + _MATRA[vow])
                pending = None
            else:
                out.append(_INDEP[vow])
            i += len(vow)
            continue
        if ch in "ṃṁ":
            if pending:
                out.append(pending + _VIRAMA + _ANUSVARA)
                pending = None
            else:
                out.append(_ANUSVARA)
            i += 1
            continue
        if ch == "ḥ":
            flush(dead=False)
            out.append(_VISARGA)
            i += 1
            continue
        if ch == "'":
            flush(dead=False)
            out.append(_AVAGRAHA)
            i += 1
            continue
        if ch == "~" or ch == "̃":
            flush(dead=False)
            out.append(_CANDRA)
            i += 1
            continue
        if ch in _ASCII_TO_DEV_DIGIT:
            flush(dead=True)
            out.append(_ASCII_TO_DEV_DIGIT[ch])
            i += 1
            continue
        flush(dead=True)
        out.append(ch)
        i += 1
    flush(dead=True)
    return nfc("".join(out))


def _deva_cluster_to_iast(cluster: str) -> str:
    return "".join(_CONS_TO_IAST.get(ch, ch) for ch in cluster)


def _deva_to_iast_builtin(text: str) -> str:
    s = nfc(text)
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "ॐ":
            out.append("oṃ")
            i += 1
            continue
        if ch == "॥":
            out.append("||")
            i += 1
            continue
        if ch == "।":
            out.append("|")
            i += 1
            continue
        if ch in _DEV_TO_ASCII_DIGIT:
            out.append(_DEV_TO_ASCII_DIGIT[ch])
            i += 1
            continue
        if ch in _INDEP_TO_IAST:
            out.append(_INDEP_TO_IAST[ch])
            i += 1
            continue
        if ch in _DEV_CONS:
            cluster = ch
            i += 1
            while i + 1 < n and s[i] == _VIRAMA and s[i + 1] in _DEV_CONS:
                cluster += s[i + 1]
                i += 2
            body = _deva_cluster_to_iast(cluster)
            if i < n and s[i] == _VIRAMA:
                out.append(body)
                i += 1
                continue
            if i < n and s[i] in _MATRA_TO_IAST:
                out.append(body + _MATRA_TO_IAST[s[i]])
                i += 1
                continue
            out.append(body + "a")
            continue
        if ch == _ANUSVARA or ch == _VEDIC_M:
            out.append("ṃ")
            i += 1
            continue
        if ch == _VISARGA:
            out.append("ḥ")
            i += 1
            continue
        if ch == _AVAGRAHA:
            out.append("'")
            i += 1
            continue
        if ch == _CANDRA:
            out.append("~")
            i += 1
            continue
        out.append(ch)
        i += 1
    return nfc("".join(out))


def iast_to_deva(text: str, *, old_deva: str = "") -> str:
    s = nfc(text)
    if _sanscript is not None:
        s = s.replace("ṁ", "ṃ").replace("Ṁ", "Ṃ")
        out = nfc(_sanscript.transliterate(s, _sanscript.IAST, _sanscript.DEVANAGARI))
    else:
        out = _iast_to_deva_builtin(s)
    if old_deva and "ऽ" in old_deva and "ऽ" in out:
        if any(i and old_deva[i - 1] != " " for i, ch in enumerate(old_deva) if ch == "ऽ"):
            out = out.replace(" ऽ", "ऽ")
    return out


def deo_to_iast(text: str) -> str:
    s = nfc(text)
    if _sanscript is not None:
        return nfc(_sanscript.transliterate(s, _sanscript.DEVANAGARI, _sanscript.IAST))
    return _deva_to_iast_builtin(s)
