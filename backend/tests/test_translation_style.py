"""Default translation template is ready to use; lock is optional."""
from types import SimpleNamespace

from app.services.translation_style import (
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
