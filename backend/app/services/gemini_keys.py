"""Pool of Google AI Studio keys: round-robin, skip daily-exhausted until PT midnight."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services.llm_status import seconds_until_pacific_midnight

log = logging.getLogger("sanskrit.gemini")

_LABEL_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]{0,63}):(.+)$")


def looks_like_studio_key(value: str) -> bool:
    text = (value or "").strip().strip('"').strip("'")
    if text.startswith("AIza") and len(text) >= 20:
        return True
    if text.startswith("AQ.") and len(text) >= 16:
        return True
    return False


def key_fingerprint(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:16]


def key_log_label(key: str, entries: list[tuple[str, str]] | None = None) -> str:
    hint = f"…{(key or '')[-4:]}" if len(key or "") >= 4 else "?"
    for label, candidate in entries or configured_studio_entries():
        if candidate == key:
            return f"{label}({hint})"
    return hint


def parse_studio_key_blob(raw: str) -> list[tuple[str, str]]:
    """Parse GEMINI_API_KEYS / a labeled key file (no secrets in logs)."""
    tokens: list[str] = []
    for part in re.split(r"[\n,;]+", raw or ""):
        tok = part.strip().strip('"').strip("'")
        if tok:
            tokens.append(tok)
    out: list[tuple[str, str]] = []
    pending_label: str | None = None
    anon = 0
    for tok in tokens:
        labeled = _LABEL_KEY.match(tok)
        if labeled and looks_like_studio_key(labeled.group(2).strip()):
            out.append((labeled.group(1).strip(), labeled.group(2).strip()))
            pending_label = None
            continue
        if looks_like_studio_key(tok):
            if pending_label:
                label = pending_label
                pending_label = None
            elif not out:
                label = "default"
            else:
                anon += 1
                label = f"key{anon}"
            out.append((label, tok))
            continue
        pending_label = tok
    return out


def parse_studio_key_file(text: str) -> list[tuple[str, str]]:
    return parse_studio_key_blob(text)


def configured_studio_entries(settings: Any | None = None) -> list[tuple[str, str]]:
    settings = settings or get_settings()
    primary = (getattr(settings, "gemini_api_key", None) or "").strip()
    extra = (getattr(settings, "gemini_api_keys", None) or "").strip()
    keys_file = (getattr(settings, "gemini_keys_file", None) or "").strip()
    parsed: list[tuple[str, str]] = []
    parsed.extend(parse_studio_key_blob(extra))
    if keys_file:
        path = Path(keys_file)
        if path.is_file():
            try:
                parsed.extend(parse_studio_key_file(path.read_text(encoding="utf-8")))
            except OSError as exc:
                log.warning("Gemini keys file unreadable: %s", exc)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    if primary:
        label = next((lb for lb, key in parsed if key == primary), "default")
        out.append((label, primary))
        seen.add(primary)
    for label, key in parsed:
        if key and key not in seen:
            out.append((label, key))
            seen.add(key)
    return out


def _pool_path(settings: Any | None = None) -> Path:
    settings = settings or get_settings()
    root = Path(settings.storage_root).parent / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root / "gemini_key_pool.json"


def _empty_state() -> dict[str, Any]:
    return {"rr": 0, "exhausted": {}}


def _load_state(settings: Any | None = None) -> dict[str, Any]:
    path = _pool_path(settings)
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    exhausted = data.get("exhausted")
    if not isinstance(exhausted, dict):
        exhausted = {}
    try:
        rr = int(data.get("rr") or 0)
    except (TypeError, ValueError):
        rr = 0
    return {"rr": rr, "exhausted": exhausted}


def _save_state(state: dict[str, Any], settings: Any | None = None) -> None:
    path = _pool_path(settings)
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        handle.write(payload)
        handle.flush()


def _exhausted_until(
    key: str,
    state: dict[str, Any] | None = None,
    *,
    settings: Any | None = None,
) -> float:
    blob = (state or _load_state(settings)).get("exhausted") or {}
    raw = blob.get(key_fingerprint(key))
    try:
        until = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    return until


def is_key_exhausted(key: str, now: float | None = None, *, settings: Any | None = None) -> bool:
    until = _exhausted_until(key, settings=settings)
    return until > (now if now is not None else time.time())


def mark_daily_exhausted(key: str, *, settings: Any | None = None) -> None:
    state = _load_state(settings)
    until = time.time() + seconds_until_pacific_midnight()
    exhausted = dict(state.get("exhausted") or {})
    fp = key_fingerprint(key)
    exhausted[fp] = until
    state["exhausted"] = exhausted
    _save_state(state, settings)
    log.warning(
        "Gemini Studio key %s marked exhausted until Pacific midnight (%.0fs)",
        key_log_label(key),
        until - time.time(),
    )


def note_studio_success(key: str, *, settings: Any | None = None) -> None:
    entries = configured_studio_entries(settings)
    if len(entries) < 2:
        return
    state = _load_state(settings)
    state["rr"] = int(state.get("rr") or 0) + 1
    _save_state(state, settings)


def available_studio_entries(settings: Any | None = None) -> list[tuple[str, str]]:
    now = time.time()
    return [
        (label, key)
        for label, key in configured_studio_entries(settings)
        if not is_key_exhausted(key, now, settings=settings)
    ]


def pick_studio_key(settings: Any | None = None) -> str:
    entries = configured_studio_entries(settings)
    if not entries:
        return ""
    avail = available_studio_entries(settings) or entries
    rr = int(_load_state(settings).get("rr") or 0)
    return avail[rr % len(avail)][1]


def studio_keys_to_try(preferred: str | None, *, settings: Any | None = None) -> list[str]:
    """Keys for one generateContent call. Explicit keys outside the pool stay single-key (tests)."""
    pref = (preferred or "").strip()
    entries = configured_studio_entries(settings)
    pool_keys = [key for _, key in entries]
    if not pool_keys:
        return [pref] if pref else []
    if pref and pref not in pool_keys:
        return [pref]
    now = time.time()
    avail = [key for key in pool_keys if not is_key_exhausted(key, now, settings=settings)]
    if not avail:
        return [pref] if pref else [pool_keys[0]]
    if pref in avail:
        idx = avail.index(pref)
        return avail[idx:] + avail[:idx]
    return avail


def pool_summary(settings: Any | None = None) -> dict[str, Any]:
    entries = configured_studio_entries(settings)
    avail = available_studio_entries(settings)
    return {
        "n": len(entries),
        "available": len(avail),
        "labels": [label for label, _ in entries],
        "available_labels": [label for label, _ in avail],
    }
