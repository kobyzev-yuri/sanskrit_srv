"""Admin-selectable LLM route: Gemini (AI Studio default) vs OpenRouter / ProxyAPI Opus."""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from app.config import get_settings

RouteId = Literal["openrouter", "gemini", "opus"]

ROUTES: dict[RouteId, dict[str, str]] = {
    "openrouter": {
        "id": "openrouter",
        "label": "OpenRouter",
        "hint": "Модель OPENROUTER_MODEL. Нужен OPENROUTER_API_KEY.",
    },
    "gemini": {
        "id": "gemini",
        "label": "Gemini 3.1 Pro (Google AI Studio)",
        "hint": "Скан + перевод через Gemini 3.1 Pro. Ключ GEMINI_API_KEY (AI Studio). ProxyAPI не нужен.",
    },
    "opus": {
        "id": "opus",
        "label": "Claude Opus (ProxyAPI, дороже)",
        "hint": "Основной: Claude Opus через ProxyAPI. Запасные: Gemini, затем OpenAI.",
    },
}


def _data_dir() -> Path:
    settings = get_settings()
    root = Path(settings.storage_root).parent / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _route_path() -> Path:
    return _data_dir() / "llm_route.json"


def _key_hint(key: str | None) -> str | None:
    text = (key or "").strip()
    return text[-4:] if len(text) >= 4 else None


@dataclass(frozen=True)
class LlmCreds:
    use_default: bool
    route: RouteId
    openrouter_api_key: str
    openai_api_key: str
    gemini_api_key: str
    user_id: uuid.UUID | None
    key_source: str  # default | personal

    @property
    def key_hint(self) -> str | None:
        if self.route == "openrouter":
            return _key_hint(self.openrouter_api_key)
        if self.route == "gemini" and self.gemini_api_key:
            return _key_hint(self.gemini_api_key)
        return _key_hint(self.openai_api_key)


_llm_creds: ContextVar[LlmCreds | None] = ContextVar("llm_creds", default=None)


def get_global_route() -> RouteId:
    """Backoffice file `data/llm_route.json` — not the expert's personal override."""
    path = _route_path()
    if not path.is_file():
        return "gemini"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        route = str(data.get("route") or "").strip()
        if route in ROUTES:
            return route  # type: ignore[return-value]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return "gemini"


def creds_from_settings(*, user_id: uuid.UUID | None = None) -> LlmCreds:
    settings = get_settings()
    return LlmCreds(
        use_default=True,
        route=get_global_route(),
        openrouter_api_key=(settings.openrouter_api_key or "").strip(),
        openai_api_key=(settings.openai_api_key or "").strip(),
        gemini_api_key=(settings.gemini_api_key or "").strip(),
        user_id=user_id,
        key_source="default",
    )


def creds_from_user(user: Any | None) -> LlmCreds:
    """Resolve keys for this user. Default = admin route + server .env, if granted."""
    if user is None:
        return creds_from_settings()
    uid = getattr(user, "id", None)
    allow = bool(getattr(user, "allow_default_llm", True))
    use_default = bool(getattr(user, "use_default_llm", True)) and allow
    if use_default:
        return creds_from_settings(user_id=uid)
    route_raw = str(getattr(user, "llm_route", None) or "").strip()
    route: RouteId = route_raw if route_raw in ROUTES else get_global_route()
    return LlmCreds(
        use_default=False,
        route=route,
        openrouter_api_key=(getattr(user, "openrouter_api_key", None) or "").strip(),
        openai_api_key=(getattr(user, "proxyapi_key", None) or "").strip(),
        gemini_api_key="",
        user_id=uid,
        key_source="personal",
    )


def current_creds() -> LlmCreds:
    return _llm_creds.get() or creds_from_settings()


def bind_llm_user(user: Any | None):
    return _llm_creds.set(creds_from_user(user))


def reset_llm_user(token) -> None:
    _llm_creds.reset(token)


@contextmanager
def llm_user_context(user: Any | None) -> Iterator[LlmCreds]:
    tok = bind_llm_user(user)
    try:
        yield current_creds()
    finally:
        reset_llm_user(tok)


def get_route() -> RouteId:
    ov = _llm_creds.get()
    if ov is not None:
        return ov.route
    return get_global_route()


def effective_openrouter_key() -> str:
    return current_creds().openrouter_api_key


def effective_proxyapi_key() -> str:
    return current_creds().openai_api_key


def require_keys_for_plan(plan: dict[str, list[str]]) -> None:
    creds = current_creds()
    personal = creds.key_source == "personal"
    if plan.get("openrouter") and not creds.openrouter_api_key:
        raise RuntimeError(
            "В кабинете не задан ключ OpenRouter (или вернитесь к токенам бэкофиса)."
            if personal
            else "OPENROUTER_API_KEY missing in server .env"
        )
    if plan.get("gemini") and not creds.gemini_api_key and not creds.openai_api_key:
        raise RuntimeError(
            "В кабинете не задан ключ ProxyAPI для Gemini (или вернитесь к токенам бэкофиса)."
            if personal
            else "GEMINI_API_KEY missing in server .env (AI Studio)"
        )
    if (plan.get("anthropic") or plan.get("openai")) and not creds.openai_api_key:
        raise RuntimeError(
            "В кабинете не задан ключ ProxyAPI (или вернитесь к токенам бэкофиса)."
            if personal
            else "OPENAI_API_KEY missing in server .env"
        )


def set_route(route: RouteId, *, updated_by: str | None = None) -> dict[str, Any]:
    if route not in ROUTES:
        raise ValueError(f"unknown route: {route}")
    payload = {
        "route": route,
        "updated_at": time.time(),
        "updated_by": updated_by,
    }
    path = _route_path()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return describe_route()


def describe_route(*, effective: bool = False) -> dict[str, Any]:
    settings = get_settings()
    creds = current_creds() if effective else creds_from_settings()
    route = creds.route if effective else get_global_route()
    meta = ROUTES[route]
    or_model = (settings.openrouter_model or "").strip() or "stealth/ox-alpha"
    opus_model = (settings.anthropic_model or "").strip() or "claude-opus-5"
    gemini_model = (settings.gemini_model or "").strip() or "gemini-3.1-pro-preview"
    openai_model = (settings.openai_model or "").strip() or "gpt-4o-mini"
    primaries = {
        "openrouter": {"provider": "openrouter", "model": or_model},
        "opus": {"provider": "anthropic", "model": opus_model},
        "gemini": {"provider": "gemini", "model": gemini_model},
    }
    options = [
        {**ROUTES["openrouter"], "primary": primaries["openrouter"]},
        {**ROUTES["gemini"], "primary": primaries["gemini"]},
        {**ROUTES["opus"], "primary": primaries["opus"]},
    ]
    return {
        "route": route,
        "label": meta["label"],
        "hint": meta["hint"],
        "options": options,
        "primary": primaries[route],
        "fallback_models": {
            "openrouter": or_model,
            "gemini": gemini_model,
            "openai": openai_model,
            "anthropic": opus_model,
        },
        "updated_at": _read_updated_at(),
        "openrouter_key": bool(creds.openrouter_api_key if effective else (settings.openrouter_api_key or "").strip()),
        "proxyapi_key": bool(creds.openai_api_key if effective else (settings.openai_api_key or "").strip()),
        "gemini_key": bool(creds.gemini_api_key if effective else (settings.gemini_api_key or "").strip()),
        "key_source": creds.key_source if effective else "default",
        "use_default": creds.use_default if effective else True,
    }


def _read_updated_at() -> float | None:
    path = _route_path()
    if not path.is_file():
        return None
    try:
        return float(json.loads(path.read_text(encoding="utf-8")).get("updated_at") or 0) or None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _empty_plan() -> dict[str, list[str]]:
    return {"openrouter": [], "anthropic": [], "gemini": [], "openai": []}


def model_plan() -> dict[str, list[str]]:
    """Ordered model lists for revise_from_scan / proofread."""
    settings = get_settings()
    route = get_route()
    plan = _empty_plan()
    or_model = (settings.openrouter_model or "").strip() or "stealth/ox-alpha"
    opus = (settings.anthropic_model or "").strip() or "claude-opus-5"
    gemini_primary = (settings.gemini_model or "").strip() or "gemini-3.1-pro-preview"
    geminis = [
        m
        for m in [gemini_primary, "gemini-3.1-pro-preview", "gemini-2.5-flash"]
        if m and not m.lower().startswith("claude")
    ]
    seen: set[str] = set()
    gemini_models: list[str] = []
    for m in geminis:
        if m not in seen:
            seen.add(m)
            gemini_models.append(m)
    openai_models: list[str] = []
    for m in [settings.openai_model, "gpt-4o-mini", "gpt-4o"]:
        if m and m not in openai_models:
            openai_models.append(m)

    if route == "openrouter":
        plan["openrouter"] = [or_model]
        return plan
    if route == "opus":
        plan["anthropic"] = [opus]
        plan["gemini"] = gemini_models
        plan["openai"] = openai_models
        return plan
    plan["gemini"] = gemini_models
    if not (current_creds().gemini_api_key or "").strip():
        plan["openai"] = openai_models
    return plan


def model_plan_primary_only() -> dict[str, list[str]]:
    """Active primary network only — no silent paid fallbacks."""
    plan = model_plan()
    out = _empty_plan()
    for key in ("openrouter", "anthropic", "gemini", "openai"):
        if plan.get(key):
            out[key] = plan[key][:1]
            break
    return out
