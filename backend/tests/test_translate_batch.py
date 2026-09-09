"""Multi-page translate grouping and ===PAGE n=== parse."""
from types import SimpleNamespace

from app.services.html_chunks import MIN_SPLIT
from app.services.llm_draft import split_batch_page_html
from app.services.llm_translate import (
    pack_translate_runs,
    page_too_large_for_batch,
    translate_batch_size_for_plan,
    translate_from_sources,
    validate_translation_html,
)
from app.services.pipeline import translate_one_by_one
from app.services.translation_style import build_translate_batch_prompt, default_translation_settings


TR = (
    '<article class="page-style" lang="ru">'
    '<p class="sa shloka" lang="sa">यज्ञो वै श्रेष्ठतमं कर्म</p>'
    '<p class="ru tr" lang="ru">Жертва — наилучшее деяние</p>'
    "</article>"
)
TR2 = (
    '<article class="page-style" lang="ru">'
    '<p class="sa shloka" lang="sa">द्वितीयः श्लोकः</p>'
    '<p class="ru tr" lang="ru">Вторая строфа перевода</p>'
    "</article>"
)
SMALL = '<article class="page-style" lang="sa"><p class="sa">क</p></article>'


def _page(no: int, src: str = SMALL):
    return SimpleNamespace(page_no=no, source_html=src)


def test_translate_batch_size_by_plan():
    assert translate_batch_size_for_plan(
        {"openrouter": ["z-ai/glm-5.3"], "anthropic": [], "gemini": [], "openai": []}
    ) == 2
    assert translate_batch_size_for_plan(
        {"openrouter": ["stealth/ox-alpha"], "anthropic": [], "gemini": [], "openai": []}
    ) == 2
    assert translate_batch_size_for_plan(
        {"openrouter": [], "anthropic": [], "gemini": ["gemini-3.1-pro-preview"], "openai": []}
    ) == 6
    assert translate_batch_size_for_plan(
        {"openrouter": [], "anthropic": [], "gemini": ["gemini-3.5-flash"], "openai": []}
    ) == 3
    assert translate_batch_size_for_plan(
        {"openrouter": [], "anthropic": ["claude-opus-5"], "gemini": [], "openai": []}
    ) == 4
    assert translate_batch_size_for_plan(
        {"openrouter": [], "anthropic": [], "gemini": [], "openai": ["gpt-4o"]}
    ) == 3


def test_pack_translate_runs_splits_gaps_and_cap():
    pages = [_page(n) for n in (3, 7, 8, 9, 40)]
    runs = pack_translate_runs(pages, max_n=6)
    assert [[p.page_no for p in r] for r in runs] == [[3], [7, 8, 9], [40]]
    assert [[p.page_no for p in r] for r in pack_translate_runs(pages, max_n=2)] == [
        [3],
        [7, 8],
        [9],
        [40],
    ]
    assert [[p.page_no for p in r] for r in pack_translate_runs(pages, max_n=1)] == [
        [3],
        [7],
        [8],
        [9],
        [40],
    ]


def test_pack_translate_runs_isolates_large_and_source_cap():
    huge_inner = "".join(f"<p class='sa'>{'क' * 80}</p>" for _ in range(120))
    huge = f'<article class="page-style" lang="sa">{huge_inner}</article>'
    assert page_too_large_for_batch(huge)
    assert len(huge) > MIN_SPLIT
    pages = [_page(10), _page(11, huge), _page(12)]
    runs = pack_translate_runs(pages, max_n=6)
    assert [[p.page_no for p in r] for r in runs] == [[10], [11], [12]]

    fat = SMALL + ("य" * 12000)
    pages = [_page(1, fat), _page(2, fat)]
    runs = pack_translate_runs(pages, max_n=6, source_cap=18000)
    assert [[p.page_no for p in r] for r in runs] == [[1], [2]]


def test_build_translate_batch_prompt_labels_pages():
    cfg = default_translation_settings()
    prompt = build_translate_batch_prompt(
        pages=[(4, SMALL), (5, SMALL)],
        cfg=cfg,
    )
    assert "===PAGE N===" in prompt
    assert "SOURCE HTML for page 4:" in prompt
    assert "SOURCE HTML for page 5:" in prompt
    assert "pages 4–5" in prompt


def test_translate_from_sources_parses_labeled_pages(monkeypatch):
    raw = f"===PAGE 20===\n{TR}\n===PAGE 21===\n{TR2}\n"
    monkeypatch.setattr(
        "app.services.llm_translate.run_text_prompt",
        lambda *_a, **_k: (raw, "gemini:gemini-3.5-flash", {"total_tokens": 100}),
    )
    out, model, _usage = translate_from_sources(
        [
            {"page_no": 20, "source_html": SMALL},
            {"page_no": 21, "source_html": SMALL},
        ],
        cfg=default_translation_settings(),
    )
    assert model.startswith("gemini:")
    assert set(out) == {20, 21}
    assert "Жертва" in validate_translation_html(out[20])
    assert "Вторая" in out[21]


def test_translate_from_sources_partial_missing_page(monkeypatch):
    raw = f"===PAGE 20===\n{TR}\n"
    monkeypatch.setattr(
        "app.services.llm_translate.run_text_prompt",
        lambda *_a, **_k: (raw, "gemini:x", {}),
    )
    out, _model, _usage = translate_from_sources(
        [
            {"page_no": 20, "source_html": SMALL},
            {"page_no": 21, "source_html": SMALL},
        ],
        cfg=default_translation_settings(),
    )
    assert set(out) == {20}


def test_split_batch_page_html_still_used_for_translate():
    raw = f"===PAGE 8===\n{TR}\n===PAGE 9===\n{TR2}\n"
    blocks = split_batch_page_html(raw)
    assert set(blocks) == {8, 9}


def test_translate_one_by_one_continues_after_failure(monkeypatch):
    calls: list[int] = []

    def fake_one(_db, page, **_kwargs):
        calls.append(page.page_no)
        if page.page_no == 31:
            raise RuntimeError("bad html")
        return f"ok:{page.page_no}"

    monkeypatch.setattr("app.services.pipeline.process_one_translate_page", fake_one)
    db = SimpleNamespace(get=lambda *_a, **_k: None)
    pages = [SimpleNamespace(id=n, page_no=n) for n in (30, 31, 32)]
    notes = translate_one_by_one(db, pages)
    assert calls == [30, 31, 32]
    assert notes[0] == "ok:30"
    assert notes[1].startswith("fail:31")
    assert notes[2] == "ok:32"
