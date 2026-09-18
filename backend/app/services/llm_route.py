"""Admin-selectable LLM route: Gemini (AI Studio default) vs Timeweb Gateway / ProxyAPI."""
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

TIMEWEB_BASE_URL = "https://api.timeweb.ai/v1"
_LEGACY_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Internal route id stays `openrouter` (llm_route.json, users.llm_route, usage network).
ROUTES: dict[RouteId, dict[str, str]] = {
    "openrouter": {
        "id": "openrouter",
        "label": "Timeweb AI Gateway",
        "hint": "Платный шлюз https://api.timeweb.ai/v1. Ключ — в кабинете Timeweb (AI Gateway) или в ЛК. Список — модели со зрением для скана.",
    },
    "gemini": {
        "id": "gemini",
        "label": "Gemini (Google AI Studio)",
        "hint": "Бесплатные ключи GEMINI_API_KEY / GEMINI_API_KEYS. 20 запросов в день на ключ; при исчерпании — следующий.",
    },
    "opus": {
        "id": "opus",
        "label": "ProxyAPI.ru (платный)",
        "hint": "Ключ OPENAI_API_KEY. Модель выбирается списком — не только Opus.",
    },
}

# Vision-capable Timeweb ids from GET /v1/models (not OpenRouter google/…).
DEFAULT_TIMEWEB_MODEL = "gemini/gemini-3.5-flash"
TIMEWEB_MODELS: list[dict[str, str]] = [
    {"id": "gemini/gemini-3.5-flash", "label": "Gemini 3.5 Flash — оцифровка"},
    {"id": "gemini/gemini-3.8-flash", "label": "Gemini 3.8 Flash"},
    {"id": "gemini/gemini-3.7-flash", "label": "Gemini 3.7 Flash"},
    {"id": "gemini/gemini-3.6-flash", "label": "Gemini 3.6 Flash"},
    {"id": "gemini/gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview — перевод / IAST"},
    {"id": "anthropic/claude-opus-5", "label": "Claude Opus 5"},
    {"id": "anthropic/claude-sonnet-5", "label": "Claude Sonnet 5"},
    {"id": "zai/glm-5.3-flash", "label": "GLM 5.3 Flash"},
    {"id": "dashscope/qwen3.8-max", "label": "Qwen 3.8 Max"},
    {"id": "dashscope/qwen3.7-plus", "label": "Qwen 3.7 Plus"},
    {"id": "dashscope/qwen3.6-flash", "label": "Qwen 3.6 Flash"},
]
_TIMEWEB_IDS = {m["id"] for m in TIMEWEB_MODELS}
_TIMEWEB_ALIASES: dict[str, str] = {
    "google/gemini-3.5-flash": "gemini/gemini-3.5-flash",
    "google/gemini-3.8-flash": "gemini/gemini-3.8-flash",
    "google/gemini-3.7-flash": "gemini/gemini-3.7-flash",
    "google/gemini-3.6-flash": "gemini/gemini-3.6-flash",
    "google/gemini-3.1-pro-preview": "gemini/gemini-3.1-pro-preview",
    "claude-5-sonnet": "anthropic/claude-sonnet-5",
    "z-ai/glm-5.3-flash": "zai/glm-5.3-flash",
    "qwen/qwen3.8-max": "dashscope/qwen3.8-max",
    "qwen/qwen3.7-plus": "dashscope/qwen3.7-plus",
    "qwen/qwen3.6-flash": "dashscope/qwen3.6-flash",
    "qwen/qwen-3.8-max": "dashscope/qwen3.8-max",
    "qwen/qwen-3.7-plus": "dashscope/qwen3.7-plus",
    "qwen/qwen-3.6-flash": "dashscope/qwen3.6-flash",
}
for _m in TIMEWEB_MODELS:
    _TIMEWEB_ALIASES[_m["id"].lower()] = _m["id"]
    _TIMEWEB_ALIASES[_m["id"].rsplit("/", 1)[-1].lower()] = _m["id"]

# Selectable on the ProxyAPI route (same OPENAI_API_KEY, different gateways).
PROXYAPI_MODELS: list[dict[str, str]] = [
    {"id": "claude-opus-5", "provider": "anthropic", "label": "Claude Opus 5"},
    {"id": "claude-opus-4-6", "provider": "anthropic", "label": "Claude Opus 4.6"},
    {"id": "gpt-4o", "provider": "openai", "label": "GPT-4o"},
    {"id": "gpt-4o-mini", "provider": "openai", "label": "GPT-4o mini"},
    {"id": "gemini-2.5-flash", "provider": "gemini", "label": "Gemini 2.5 Flash (Google на ProxyAPI)"},
    {"id": "gemini-3.1-pro-preview", "provider": "gemini", "label": "Gemini 3.1 Pro (Google на ProxyAPI)"},
    {"id": "gemini-3.5-flash", "provider": "gemini", "label": "Gemini 3.5 Flash (Google на ProxyAPI)"},
]


def _data_dir() -> Path:
    settings = get_settings()
    root = Path(settings.storage_root).parent / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _route_path() -> Path:
    return _data_dir() / "llm_route.json"


def _read_state() -> dict[str, Any]:
    path = _route_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def sanitize_openrouter_model(raw: str | None) -> str:
    """Map a Timeweb vision catalog id. Drop ox-alpha and unknown free-typed ids."""
    text = (raw or "").strip()
    if not text or "ox-alpha" in text.lower() or text.lower().startswith("stealth/"):
        return ""
    canonical = _TIMEWEB_ALIASES.get(text.lower(), text)
    return canonical if canonical in _TIMEWEB_IDS else ""


def settings_gateway_key(settings: Any | None = None) -> str:
    s = settings or get_settings()
    return (
        (getattr(s, "timeweb_api_key", None) or "").strip()
        or (getattr(s, "openrouter_api_key", None) or "").strip()
    )


def settings_gateway_model(settings: Any | None = None) -> str:
    s = settings or get_settings()
    return sanitize_openrouter_model(
        (getattr(s, "timeweb_model", None) or "").strip()
        or (getattr(s, "openrouter_model", None) or "").strip()
    )


def gateway_batch_pages(model: str, *, vision: bool) -> int:
    """Pages per call for a Timeweb / gateway model id."""
    blob = (model or "").lower()
    if not blob:
        return 1
    if "ox-alpha" in blob or blob.startswith("stealth/"):
        return 1 if vision else 2
    if "gemini" in blob:
        return 3 if "flash" in blob else 6
    if "claude" in blob or "anthropic" in blob:
        return 4
    if "glm" in blob or "qwen" in blob:
        return 3
    if "gpt" in blob or "openai" in blob:
        return 3
    return 1


def _default_proxyapi_model() -> str:
    settings = get_settings()
    return (settings.anthropic_model or "").strip() or "claude-opus-5"


def classify_proxyapi_model(model: str | None) -> tuple[str, str]:
    """Return (plan_key, model_id) for a ProxyAPI catalog id."""
    text = (model or "").strip() or _default_proxyapi_model()
    low = text.lower()
    if low.startswith("claude"):
        return "anthropic", text
    if low.startswith("gpt") or low.startswith(("o1", "o3", "o4")):
        return "openai", text
    if "gemini" in low:
        return "gemini", text
    return "openai", text


def get_proxyapi_model() -> str:
    raw = str(_read_state().get("proxyapi_model") or "").strip()
    return raw or _default_proxyapi_model()


def gemini_uses_proxyapi() -> bool:
    """True when the active route is ProxyAPI and the picked model is Gemini."""
    if get_route() != "opus":
        return False
    kind, _ = classify_proxyapi_model(get_proxyapi_model())
    return kind == "gemini"


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
    openrouter_model: str
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
    route = str(_read_state().get("route") or "").strip()
    if route in ROUTES:
        return route  # type: ignore[return-value]
    return "gemini"


def creds_from_settings(*, user_id: uuid.UUID | None = None) -> LlmCreds:
    from app.services.gemini_keys import pick_studio_key

    settings = get_settings()
    return LlmCreds(
        use_default=True,
        route=get_global_route(),
        openrouter_api_key=settings_gateway_key(settings),
        openai_api_key=(settings.openai_api_key or "").strip(),
        gemini_api_key=pick_studio_key(settings) or (settings.gemini_api_key or "").strip(),
        openrouter_model=get_timeweb_model(),
        user_id=user_id,
        key_source="default",
    )


def infer_personal_route(user: Any) -> RouteId:
    """Personal network: stored route, else Timeweb/ProxyAPI key, else admin file.

    Saving a Timeweb key used to leave `users.llm_route` NULL. Falling back to the
    admin Gemini file then ignored the expert's Gateway key.
    """
    route_raw = str(getattr(user, "llm_route", None) or "").strip()
    if route_raw in ROUTES:
        return route_raw  # type: ignore[return-value]
    if (getattr(user, "openrouter_api_key", None) or "").strip():
        return "openrouter"
    if (getattr(user, "proxyapi_key", None) or "").strip():
        return "opus"
    return get_global_route()


def heal_personal_llm_route(user: Any) -> None:
    """Persist inferred network when the expert left backoffice tokens."""
    if bool(getattr(user, "use_default_llm", True)):
        return
    route_raw = str(getattr(user, "llm_route", None) or "").strip()
    if route_raw in ROUTES:
        return
    if (getattr(user, "openrouter_api_key", None) or "").strip():
        user.llm_route = "openrouter"
    elif (getattr(user, "proxyapi_key", None) or "").strip():
        user.llm_route = "opus"


def creds_from_user(user: Any | None) -> LlmCreds:
    """Resolve keys for this user. Default = admin route + server .env, if granted."""
    if user is None:
        return creds_from_settings()
    uid = getattr(user, "id", None)
    allow = bool(getattr(user, "allow_default_llm", True))
    use_default = bool(getattr(user, "use_default_llm", True)) and allow
    if use_default:
        return creds_from_settings(user_id=uid)
    return LlmCreds(
        use_default=False,
        route=infer_personal_route(user),
        openrouter_api_key=(getattr(user, "openrouter_api_key", None) or "").strip(),
        openai_api_key=(getattr(user, "proxyapi_key", None) or "").strip(),
        gemini_api_key="",
        openrouter_model=sanitize_openrouter_model(getattr(user, "openrouter_model", None)),
        user_id=uid,
        key_source="personal",
    )


def current_creds() -> LlmCreds:
    return _llm_creds.get() or creds_from_settings()


def bind_llm_user(user: Any | None):
    return _llm_creds.set(creds_from_user(user))


def reset_llm_user(token) -> None:
    try:
        _llm_creds.reset(token)
    except ValueError:
        # FastAPI may close the sync dependency in a different ContextVar context.
        _llm_creds.set(None)


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


def get_timeweb_model() -> str:
    raw = str(_read_state().get("openrouter_model") or _read_state().get("timeweb_model") or "").strip()
    picked = sanitize_openrouter_model(raw) or settings_gateway_model()
    return picked or DEFAULT_TIMEWEB_MODEL


def effective_openrouter_base_url() -> str:
    settings = get_settings()
    raw = (settings.openrouter_base_url or "").strip().rstrip("/")
    if not raw or raw == _LEGACY_OPENROUTER_BASE.rstrip("/"):
        return TIMEWEB_BASE_URL
    return raw


def effective_proxyapi_key() -> str:
    return current_creds().openai_api_key


def require_keys_for_plan(plan: dict[str, list[str]]) -> None:
    creds = current_creds()
    personal = creds.key_source == "personal"
    if get_route() == "openrouter" and not plan.get("openrouter"):
        raise RuntimeError(
            "Модель Timeweb не задана. Выберите сеть со зрением в кабинете."
            if personal
            else "Модель Timeweb не задана. Выберите её в бэкофисе (Маршрут LLM) или TIMEWEB_MODEL в .env."
        )
    if plan.get("openrouter"):
        if not creds.openrouter_api_key:
            raise RuntimeError(
                "В кабинете не задан ключ Timeweb AI Gateway (или вернитесь к токенам бэкофиса)."
                if personal
                else "TIMEWEB_API_KEY (или OPENROUTER_API_KEY) missing in server .env"
            )
    if plan.get("gemini"):
        if gemini_uses_proxyapi():
            if not creds.openai_api_key:
                raise RuntimeError(
                    "В кабинете не задан ключ ProxyAPI для Gemini (или вернитесь к токенам бэкофиса)."
                    if personal
                    else "OPENAI_API_KEY missing in server .env (ProxyAPI Google)"
                )
        elif not creds.gemini_api_key and not creds.openai_api_key:
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


def set_route(
    route: RouteId,
    *,
    proxyapi_model: str | None = None,
    openrouter_model: str | None = None,
    updated_by: str | None = None,
) -> dict[str, Any]:
    if route not in ROUTES:
        raise ValueError(f"unknown route: {route}")
    prev = _read_state()
    model = (proxyapi_model or "").strip() or str(prev.get("proxyapi_model") or "").strip()
    if not model:
        model = _default_proxyapi_model()
    tw_raw = openrouter_model if openrouter_model is not None else str(
        prev.get("openrouter_model") or prev.get("timeweb_model") or ""
    )
    tw_model = sanitize_openrouter_model(tw_raw) or settings_gateway_model() or DEFAULT_TIMEWEB_MODEL
    payload = {
        "route": route,
        "proxyapi_model": model,
        "openrouter_model": tw_model,
        "updated_at": time.time(),
        "updated_by": updated_by,
    }
    path = _route_path()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return describe_route()


def describe_route(*, effective: bool = False) -> dict[str, Any]:
    from app.services.gemini_keys import configured_studio_entries, pool_summary

    settings = get_settings()
    creds = current_creds() if effective else creds_from_settings()
    gemini_pool = pool_summary(settings)
    route = creds.route if effective else get_global_route()
    or_model = (
        sanitize_openrouter_model(creds.openrouter_model) or get_timeweb_model()
        if effective
        else get_timeweb_model()
    )
    gemini_model = (settings.gemini_model or "").strip() or "gemini-3.1-pro-preview"
    gemini_translate_model = (
        (getattr(settings, "gemini_translate_model", None) or "").strip() or "gemini-3.1-pro-preview"
    )
    openai_model = (settings.openai_model or "").strip() or "gpt-4o-mini"
    px_model = get_proxyapi_model()
    px_kind, px_id = classify_proxyapi_model(px_model)
    gemini_meta = {
        **ROUTES["gemini"],
        "label": (
            f"Gemini (Google AI Studio) — {gemini_model}"
            + (f" · перевод {gemini_translate_model}" if gemini_translate_model != gemini_model else "")
        ),
        "hint": "Бесплатные ключи GEMINI_API_KEY / GEMINI_API_KEYS. 20 запросов в день на ключ; при исчерпании — следующий. ProxyAPI здесь не используется. Оцифровка — GEMINI_MODEL (зрение); перевод — GEMINI_TRANSLATE_MODEL (текст).",
    }
    opus_meta = {
        **ROUTES["opus"],
        "hint": f"Ключ OPENAI_API_KEY. Сейчас {px_kind}:{px_id}. Список моделей — под радиокнопками.",
    }
    or_meta = {
        **ROUTES["openrouter"],
        "hint": (
            f"Ключ TIMEWEB_API_KEY. Сейчас {or_model}. Список — модели со зрением (скан)."
            if or_model
            else ROUTES["openrouter"]["hint"]
        ),
    }
    primaries = {
        "openrouter": {"provider": "timeweb", "model": or_model},
        "opus": {"provider": px_kind, "model": px_id},
        "gemini": {"provider": "gemini", "model": gemini_model},
    }
    meta = {
        "openrouter": or_meta,
        "gemini": gemini_meta,
        "opus": opus_meta,
    }[route]
    options = [
        {**or_meta, "primary": primaries["openrouter"]},
        {**gemini_meta, "primary": primaries["gemini"]},
        {**opus_meta, "primary": primaries["opus"]},
    ]
    return {
        "route": route,
        "label": meta["label"],
        "hint": meta["hint"],
        "options": options,
        "primary": primaries[route],
        "proxyapi_model": px_id,
        "proxyapi_models": list(PROXYAPI_MODELS),
        "openrouter_model": or_model,
        "timeweb_model": or_model,
        "timeweb_models": list(TIMEWEB_MODELS),
        "fallback_models": {
            "openrouter": or_model,
            "gemini": gemini_model,
            "gemini_translate": gemini_translate_model,
            "openai": openai_model,
            "anthropic": _default_proxyapi_model(),
        },
        "updated_at": _read_updated_at(),
        "openrouter_key": bool(creds.openrouter_api_key if effective else settings_gateway_key(settings)),
        "proxyapi_key": bool(creds.openai_api_key if effective else (settings.openai_api_key or "").strip()),
        "gemini_key": bool(
            (creds.gemini_api_key if effective else (settings.gemini_api_key or "").strip())
            or configured_studio_entries(settings)
        ),
        "gemini_keys": gemini_pool["n"],
        "gemini_keys_available": gemini_pool["available"],
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


def model_plan(*, text: bool = False) -> dict[str, list[str]]:
    """Ordered model lists for revise_from_scan / proofread / translate.

    text=True: no images (Russian translation, translation proofread).
    """
    settings = get_settings()
    route = get_route()
    plan = _empty_plan()
    or_model = sanitize_openrouter_model(current_creds().openrouter_model) or get_timeweb_model()
    gemini_primary = (settings.gemini_model or "").strip() or "gemini-3.1-pro-preview"
    gemini_translate = (getattr(settings, "gemini_translate_model", None) or "").strip() or "gemini-3.1-pro-preview"
    if text:
        gemini_candidates = [gemini_translate, gemini_primary, "gemini-3.5-flash"]
    else:
        gemini_candidates = [gemini_primary, "gemini-3.5-flash"]
    geminis = [m for m in gemini_candidates if m and not m.lower().startswith("claude")]
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
        if or_model:
            plan["openrouter"] = [or_model]
        return plan
    if route == "opus":
        kind, model = classify_proxyapi_model(get_proxyapi_model())
        plan[kind] = [model]
        return plan
    plan["gemini"] = gemini_models
    if not (current_creds().gemini_api_key or "").strip():
        plan["openai"] = openai_models
    return plan


def model_plan_primary_only(*, text: bool = False) -> dict[str, list[str]]:
    """Active primary network only — no silent paid fallbacks.

    For Gemini text (translate), keep Studio fallbacks on the same keys (Pro → Flash).
    """
    plan = model_plan(text=text)
    out = _empty_plan()
    for key in ("openrouter", "anthropic", "gemini", "openai"):
        if plan.get(key):
            out[key] = list(plan[key] if key == "gemini" and text else plan[key][:1])
            break
    return out
