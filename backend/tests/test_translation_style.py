"""Default translation template is ready to use; lock is optional."""
from types import SimpleNamespace

from app.services.translation_style import (
    NOTES_MAX,
    STYLE_IAST_GLOSS,
    STYLES,
    build_translate_messages,
    build_translate_prompt,
    default_translation_settings,
    lock_translation_template,
    translation_agreed,
    translation_cfg,
)


def test_default_template_is_agreed():
    cfg = default_translation_settings()
    assert cfg["agreed"] is True
    assert cfg["style"] == "interlinear"


def test_lock_unlocks_legacy_false_flag():
    project = SimpleNamespace(
        settings={"task": "translate", "translation": {"style": "interlinear", "agreed": False}}
    )
    assert translation_agreed(project) is False
    lock_translation_template(project, SimpleNamespace(id="u1"))
    assert translation_agreed(project) is True
    assert translation_cfg(project)["agreed_by"] == "u1"


def test_iast_gloss_is_a_known_style():
    assert STYLE_IAST_GLOSS in STYLES
    cfg = default_translation_settings(style=STYLE_IAST_GLOSS)
    assert cfg["style"] == STYLE_IAST_GLOSS
    src = '<article class="page-style" lang="sa"><p class="sa">यज्ञः</p></article>'
    system, user = build_translate_messages(source_html=src, cfg=cfg)
    assert "кашмирск" in system
    assert "padam:" in system
    assert "yuktyā" in system
    assert "пратипадам" in system
    assert "Не выводи грамматический разбор" in system
    assert "каждое слово оригинала на наличие в переводе" in system
    assert src in user
    assert "ОРИГИНАЛ СТРАНИЦЫ" in user
    assert "кашмирск" not in user
    assert src not in system
    prompt = build_translate_prompt(source_html=src, cfg=cfg)
    assert prompt.startswith(system)
    assert user in prompt


def test_iast_expert_notes_go_to_system():
    cfg = default_translation_settings(style=STYLE_IAST_GLOSS, notes="spanda: пульсация")
    src = "<p class='sa'>क</p>"
    system, user = build_translate_messages(source_html=src, cfg=cfg)
    assert "ДОПОЛНИТЕЛЬНЫЙ СЛОВАРЬ" in system
    assert "spanda: пульсация" in system
    assert "spanda: пульсация" not in user


def test_notes_keep_long_prompt():
    cfg = default_translation_settings(notes="я" * 5000)
    assert len(cfg["notes"]) == 5000
    cfg = default_translation_settings(notes="я" * (NOTES_MAX + 100))
    assert len(cfg["notes"]) == NOTES_MAX
