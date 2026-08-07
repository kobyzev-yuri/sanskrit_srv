"""Admin-selectable LLM route: cheap Gemini vs expensive Claude Opus."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

from app.config import get_settings

RouteId = Literal["gemini", "opus"]

ROUTES: dict[RouteId, dict[str, str]] = {
    "gemini": {
        "id": "gemini",
        "label": "Gemini (дешевле)",
        "hint": "Основной: Gemini Flash. Запасной: OpenAI. Claude не вызывается.",
    },
    "opus": {
        "id": "opus",
        "label": "Claude Opus (дороже)",
        "hint": "Основной: Claude Opus. Запасные: Gemini, затем OpenAI.",
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
        # Prefer Opus if configured in .env, else Gemini.
        settings = get_settings()
        return "opus" if (settings.anthropic_model or "").strip() else "gemini"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        route = str(data.get("route") or "").strip()
        if route in ROUTES:
            return route  # type: ignore[return-value]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return "gemini"


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
    opus_model = (settings.anthropic_model or "").strip() or "claude-opus-5"
    gemini_model = (settings.gemini_model or "").strip() or "gemini-2.5-flash"
    openai_model = (settings.openai_model or "").strip() or "gpt-4o-mini"
    primary = (
        {"provider": "anthropic", "model": opus_model}
        if route == "opus"
        else {"provider": "gemini", "model": gemini_model}
    )
    return {
        "route": route,
        "label": meta["label"],
        "hint": meta["hint"],
        "options": [
            {
                **ROUTES["gemini"],
                "primary": {"provider": "gemini", "model": gemini_model},
            },
            {
                **ROUTES["opus"],
                "primary": {"provider": "anthropic", "model": opus_model},
            },
        ],
        "primary": primary,
        "fallback_models": {
            "gemini": gemini_model,
            "openai": openai_model,
            "anthropic": opus_model,
        },
        "updated_at": _read_updated_at(),
    }


def _read_updated_at() -> float | None:
    path = _route_path()
    if not path.is_file():
        return None
    try:
        return float(json.loads(path.read_text(encoding="utf-8")).get("updated_at") or 0) or None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def model_plan() -> dict[str, list[str]]:
    """Ordered model lists for revise_from_scan."""
    settings = get_settings()
    route = get_route()
    opus = (settings.anthropic_model or "").strip() or "claude-opus-5"
    gemini_primary = (settings.gemini_model or "").strip()
    geminis = [
        m
        for m in [gemini_primary, "gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.0-flash"]
        if m and not m.lower().startswith("claude")
    ]
    # uniq preserve order
    seen: set[str] = set()
    gemini_models: list[str] = []
    for m in geminis:
        if m not in seen:
            seen.add(m)
            gemini_models.append(m)
    openai_models = []
    for m in [settings.openai_model, "gpt-4o-mini", "gpt-4o"]:
        if m and m not in openai_models:
            openai_models.append(m)

    if route == "opus":
        return {
            "anthropic": [opus],
            "gemini": gemini_models,
            "openai": openai_models,
        }
    return {
        "anthropic": [],
        "gemini": gemini_models,
        "openai": openai_models,
    }
