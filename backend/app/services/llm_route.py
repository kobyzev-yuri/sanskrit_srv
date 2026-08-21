"""Admin-selectable LLM route: OpenRouter ox-alpha (default) vs ProxyAPI Gemini/Opus."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

from app.config import get_settings

RouteId = Literal["openrouter", "gemini", "opus"]

ROUTES: dict[RouteId, dict[str, str]] = {
    "openrouter": {
        "id": "openrouter",
        "label": "Ox Alpha (OpenRouter, бесплатно)",
        "hint": "stealth/ox-alpha: текст + картинка скана, контекст 1M. ProxyAPI не вызывается.",
    },
    "gemini": {
        "id": "gemini",
        "label": "Gemini (ProxyAPI, платно)",
        "hint": "Основной: Gemini Flash через ProxyAPI. Запасной: OpenAI. Claude не вызывается.",
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


def get_route() -> RouteId:
    path = _route_path()
    if not path.is_file():
        return "openrouter"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        route = str(data.get("route") or "").strip()
        if route in ROUTES:
            return route  # type: ignore[return-value]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return "openrouter"


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


def describe_route() -> dict[str, Any]:
    settings = get_settings()
    route = get_route()
    meta = ROUTES[route]
    or_model = (settings.openrouter_model or "").strip() or "stealth/ox-alpha"
    opus_model = (settings.anthropic_model or "").strip() or "claude-opus-5"
    gemini_model = (settings.gemini_model or "").strip() or "gemini-2.5-flash"
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
        "openrouter_key": bool((settings.openrouter_api_key or "").strip()),
        "proxyapi_key": bool((settings.openai_api_key or "").strip()),
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
    gemini_primary = (settings.gemini_model or "").strip()
    geminis = [
        m
        for m in [gemini_primary, "gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.0-flash"]
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
