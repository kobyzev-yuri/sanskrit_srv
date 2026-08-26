"""Per-user LLM credentials vs backoffice defaults."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.llm_route import (
    creds_from_user,
    get_route,
    llm_user_context,
    model_plan_primary_only,
    require_keys_for_plan,
)


def _user(**kwargs):
    defaults = dict(
        id=uuid4(),
        allow_default_llm=True,
        use_default_llm=True,
        llm_route=None,
        openrouter_api_key=None,
        proxyapi_key=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _settings(tmp_path, **kwargs):
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    defaults = dict(
        storage_root=storage,
        openrouter_model="stealth/ox-alpha",
        anthropic_model="claude-opus-5",
        gemini_model="gemini-2.5-pro",
        gemini_api_key="studio-admin",
        openai_model="gpt-4o-mini",
        openrouter_api_key="sk-or-admin",
        openai_api_key="px-admin",
        openrouter_base_url="https://openrouter.ai/api/v1",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_default_user_inherits_admin_keys(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(lr, "get_settings", lambda: _settings(tmp_path))
    creds = creds_from_user(_user())
    assert creds.use_default is True
    assert creds.key_source == "default"
    assert creds.openrouter_api_key == "sk-or-admin"
    assert creds.gemini_api_key == "studio-admin"
    assert creds.key_hint == "dmin"


def test_personal_keys_override_route(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(lr, "get_settings", lambda: _settings(tmp_path))
    user = _user(
        use_default_llm=False,
        llm_route="gemini",
        proxyapi_key="px-expert-9999",
        openrouter_api_key="sk-or-expert",
    )
    with llm_user_context(user):
        assert get_route() == "gemini"
        plan = model_plan_primary_only()
        assert plan["gemini"][:1] == ["gemini-2.5-pro"]
        assert plan["openrouter"] == []
        creds = creds_from_user(user)
        assert creds.key_source == "personal"
        assert creds.openai_api_key.endswith("9999")
        assert creds.key_hint == "9999"


def test_denied_default_requires_own_key(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(lr, "get_settings", lambda: _settings(tmp_path))
    user = _user(allow_default_llm=False, use_default_llm=True, llm_route="openrouter")
    creds = creds_from_user(user)
    assert creds.use_default is False
    assert creds.key_source == "personal"
    assert creds.openrouter_api_key == ""
    with llm_user_context(user):
        with pytest.raises(RuntimeError, match="кабинете"):
            require_keys_for_plan({"openrouter": ["stealth/ox-alpha"], "anthropic": [], "gemini": [], "openai": []})


def test_studio_gemini_does_not_need_proxyapi(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(
        lr,
        "get_settings",
        lambda: _settings(tmp_path, openai_api_key="", gemini_api_key="studio-only"),
    )
    with llm_user_context(_user()):
        require_keys_for_plan({"openrouter": [], "anthropic": [], "gemini": ["gemini-2.5-pro"], "openai": []})
