"""LLM route defaults: OpenRouter ox-alpha vs ProxyAPI Gemini/Opus."""
from __future__ import annotations

from types import SimpleNamespace

from app.services.llm_draft import _openai_message_text
from app.services.llm_route import describe_route, get_route, model_plan, model_plan_primary_only, set_route


def _settings(tmp_path, **kwargs):
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    defaults = dict(
        storage_root=storage,
        openrouter_model="stealth/ox-alpha",
        anthropic_model="claude-opus-5",
        gemini_model="gemini-2.5-flash",
        openai_model="gpt-4o-mini",
        openrouter_api_key="sk-or-test",
        openai_api_key="",
        openrouter_base_url="https://openrouter.ai/api/v1",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_default_route_is_openrouter(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(lr, "get_settings", lambda: _settings(tmp_path))
    assert get_route() == "openrouter"
    plan = model_plan()
    assert plan["openrouter"] == ["stealth/ox-alpha"]
    assert plan["anthropic"] == []
    assert plan["gemini"] == []
    desc = describe_route()
    assert desc["primary"] == {"provider": "openrouter", "model": "stealth/ox-alpha"}
    assert [o["id"] for o in desc["options"]] == ["openrouter", "gemini", "opus"]


def test_opus_route_uses_proxyapi(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(lr, "get_settings", lambda: _settings(tmp_path, openai_api_key="px"))
    set_route("opus", updated_by="t")
    assert get_route() == "opus"
    plan = model_plan()
    assert plan["anthropic"] == ["claude-opus-5"]
    assert plan["openrouter"] == []
    assert "gemini-2.5-flash" in plan["gemini"]


def test_primary_only_follows_saved_route(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(lr, "get_settings", lambda: _settings(tmp_path, openai_api_key="px"))
    set_route("gemini", updated_by="t")
    plan = model_plan_primary_only()
    assert plan["gemini"][:1] == ["gemini-2.5-flash"]
    assert plan["openrouter"] == []
    assert plan["anthropic"] == []
    set_route("openrouter", updated_by="t")
    plan = model_plan_primary_only()
    assert plan["openrouter"] == ["stealth/ox-alpha"]
    assert plan["gemini"] == []


def test_openai_message_text_reasoning_fallback():
    assert _openai_message_text({"content": "<article></article>"}) == "<article></article>"
    assert _openai_message_text({"content": "", "reasoning": "<article class='x'>"}) == "<article class='x'>"
    assert "hi" in _openai_message_text({"content": [{"type": "text", "text": "hi"}]})
    cot = "Let me analyze the source HTML.\nKeep Devanagari exactly as in source"
    assert _openai_message_text({"content": "", "reasoning": cot}) == ""
