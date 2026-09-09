"""LLM route defaults: Gemini AI Studio vs OpenRouter / ProxyAPI Opus."""
from __future__ import annotations

from types import SimpleNamespace

from app.services.llm_draft import _openai_message_text
from app.services.llm_route import (
    describe_route,
    get_proxyapi_model,
    get_route,
    model_plan,
    model_plan_primary_only,
    set_route,
)


def _settings(tmp_path, **kwargs):
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    defaults = dict(
        storage_root=storage,
        openrouter_model="stealth/ox-alpha",
        anthropic_model="claude-opus-5",
        gemini_model="gemini-3.1-pro-preview",
        gemini_api_key="studio-test",
        gemini_base_url="https://generativelanguage.googleapis.com",
        openai_model="gpt-4o-mini",
        openrouter_api_key="sk-or-test",
        openai_api_key="",
        openrouter_base_url="https://openrouter.ai/api/v1",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_default_route_is_gemini(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(lr, "get_settings", lambda: _settings(tmp_path))
    assert get_route() == "gemini"
    plan = model_plan()
    assert plan["gemini"][:1] == ["gemini-3.1-pro-preview"]
    assert plan["openrouter"] == []
    assert plan["openai"] == []
    desc = describe_route()
    assert desc["primary"] == {"provider": "gemini", "model": "gemini-3.1-pro-preview"}
    assert [o["id"] for o in desc["options"]] == ["openrouter", "gemini", "glm", "opus"]


def test_opus_route_uses_proxyapi(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(lr, "get_settings", lambda: _settings(tmp_path, openai_api_key="px"))
    set_route("opus", updated_by="t")
    assert get_route() == "opus"
    plan = model_plan()
    assert plan["anthropic"] == ["claude-opus-5"]
    assert plan["openrouter"] == []
    assert plan["gemini"] == []
    assert plan["openai"] == []


def test_proxyapi_model_picker(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(lr, "get_settings", lambda: _settings(tmp_path, openai_api_key="px"))
    set_route("opus", proxyapi_model="gpt-4o", updated_by="t")
    plan = model_plan_primary_only()
    assert plan["openai"] == ["gpt-4o"]
    assert plan["anthropic"] == []
    desc = describe_route()
    assert desc["primary"] == {"provider": "openai", "model": "gpt-4o"}
    assert desc["proxyapi_model"] == "gpt-4o"
    set_route("opus", proxyapi_model="gemini-2.5-flash", updated_by="t")
    plan = model_plan_primary_only()
    assert plan["gemini"] == ["gemini-2.5-flash"]
    assert plan["openai"] == []
    assert lr.gemini_uses_proxyapi() is True
    set_route("gemini", updated_by="t")
    assert lr.gemini_uses_proxyapi() is False
    assert get_proxyapi_model() == "gemini-2.5-flash"


def test_primary_only_follows_saved_route(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(lr, "get_settings", lambda: _settings(tmp_path, openai_api_key="px"))
    set_route("gemini", updated_by="t")
    plan = model_plan_primary_only()
    assert plan["gemini"][:1] == ["gemini-3.1-pro-preview"]
    assert plan["openrouter"] == []
    assert plan["anthropic"] == []
    set_route("openrouter", updated_by="t")
    plan = model_plan_primary_only()
    assert plan["openrouter"] == []
    assert plan["gemini"] == []
    desc = describe_route()
    assert desc["openrouter_model"] == ""
    assert desc["primary"]["model"] == ""


def test_openrouter_typed_model(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(
        lr,
        "get_settings",
        lambda: _settings(tmp_path, openrouter_model="google/gemini-2.5-flash"),
    )
    set_route("openrouter", updated_by="t")
    plan = model_plan_primary_only()
    assert plan["openrouter"] == ["google/gemini-2.5-flash"]
    assert describe_route()["openrouter_model"] == "google/gemini-2.5-flash"


def test_glm_route_uses_haimaker_not_gemini(tmp_path, monkeypatch):
    from app.services import llm_route as lr

    monkeypatch.setattr(
        lr,
        "get_settings",
        lambda: _settings(
            tmp_path,
            haimaker_api_key="sk-hm-test",
            haimaker_base_url="https://api.haimaker.ai/v1",
            haimaker_model="z-ai/glm-5v-turbo",
        ),
    )
    set_route("gemini", updated_by="t")
    assert get_route() == "gemini"
    set_route("glm", updated_by="t")
    assert get_route() == "glm"
    plan = model_plan_primary_only()
    assert plan["openrouter"] == ["z-ai/glm-5v-turbo"]
    assert plan["gemini"] == []
    assert plan["anthropic"] == []
    desc = describe_route()
    assert desc["primary"] == {"provider": "haimaker", "model": "z-ai/glm-5v-turbo"}
    assert lr.effective_openrouter_base_url() == "https://api.haimaker.ai/v1"
    assert lr.effective_openrouter_key() == "sk-hm-test"
    set_route("gemini", updated_by="t")
    assert get_route() == "gemini"
    assert model_plan_primary_only()["gemini"][:1] == ["gemini-3.1-pro-preview"]
    assert lr.effective_openrouter_base_url() == "https://openrouter.ai/api/v1"


def test_openai_message_text_reasoning_fallback():
    assert _openai_message_text({"content": "<article></article>"}) == "<article></article>"
    assert _openai_message_text({"content": "", "reasoning": "<article class='x'>"}) == "<article class='x'>"
    assert "hi" in _openai_message_text({"content": [{"type": "text", "text": "hi"}]})
    cot = "Let me analyze the source HTML.\nKeep Devanagari exactly as in source"
    assert _openai_message_text({"content": "", "reasoning": cot}) == ""
    html = "<article class='page-style'><p class='sa'>नमः</p></article>"
    assert _openai_message_text({"content": "", "reasoning_content": html}) == html
    proof = '{"suggestions":[{"id":"1","wrong":"a","right":"b"}]}'
    assert proof in _openai_message_text({"content": "", "reasoning_content": proof})


def test_reset_llm_user_foreign_context():
    from contextvars import copy_context

    from app.services import llm_route as lr

    holder: dict = {}

    def bind_in_child():
        holder["tok"] = lr._llm_creds.set(None)

    copy_context().run(bind_in_child)
    lr.reset_llm_user(holder["tok"])
