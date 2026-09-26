"""Multi-page digitize grouping and ===PAGE n=== parse."""
from types import SimpleNamespace

from app.services.llm_draft import (
    consecutive_page_runs,
    digitize_batch_size_for_plan,
    split_batch_page_html,
)
from app.services.pipeline import digitize_one_by_one


def test_digitize_prompt_is_line_transcription():
    from app.services.llm_draft import BASE_PROMPT

    assert "One printed line" in BASE_PROMPT
    assert "त्त is त्+त, not त" in BASE_PROMPT
    assert "type-lg" not in BASE_PROMPT
    assert "lh-tight" not in BASE_PROMPT


def test_split_batch_page_html():
    raw = """
commentary
===PAGE 12===
<article class="page-style type-md" lang="sa"><p class="sa">एक</p></article>
===PAGE 13===
<article class="page-style type-md" lang="sa"><p class="sa">द्वि</p></article>
"""
    blocks = split_batch_page_html(raw)
    assert 12 in blocks and 13 in blocks
    assert "एक" in blocks[12]
    assert "द्वि" in blocks[13]
    assert split_batch_page_html("<article>no marks</article>") == {}


def test_split_batch_page_html_markdown_and_articles():
    raw = """
**===PAGE 4===**
<article class="page-style" lang="sa"><p class="sa">क</p></article>
=== PAGE 5 ===
<article class="page-style" lang="sa"><p class="sa">ख</p></article>
"""
    blocks = split_batch_page_html(raw)
    assert set(blocks) == {4, 5}
    unlabeled = """
<article class="page-style" lang="sa"><p class="sa">एक</p></article>
<article class="page-style" lang="sa"><p class="sa">द्वि</p></article>
"""
    zipped = split_batch_page_html(unlabeled, page_nos=[20, 21])
    assert set(zipped) == {20, 21}
    assert "एक" in zipped[20]
    assert split_batch_page_html(unlabeled, page_nos=[20, 21, 22]) == {}


def test_consecutive_page_runs_splits_gaps_and_cap():
    assert consecutive_page_runs([3, 7, 8, 9, 40], max_n=6) == [
        [3],
        [7, 8, 9],
        [40],
    ]
    assert consecutive_page_runs([1, 2, 3, 4, 5], max_n=2) == [[1, 2], [3, 4], [5]]
    assert consecutive_page_runs([10, 11], max_n=1) == [[10], [11]]


def test_digitize_batch_size_by_plan():
    assert digitize_batch_size_for_plan({"openrouter": ["google/gemini-3.5-flash"], "anthropic": [], "gemini": [], "openai": []}) == 3
    assert digitize_batch_size_for_plan({"openrouter": ["google/gemini-3.1-pro-preview"], "anthropic": [], "gemini": [], "openai": []}) == 6
    assert digitize_batch_size_for_plan({"openrouter": ["z-ai/glm-5.3-flash"], "anthropic": [], "gemini": [], "openai": []}) == 3
    assert digitize_batch_size_for_plan({"openrouter": [], "anthropic": [], "gemini": ["gemini-3.1-pro-preview"], "openai": []}) == 6
    assert digitize_batch_size_for_plan({"openrouter": [], "anthropic": [], "gemini": ["gemini-3.5-flash"], "openai": []}) == 3
    assert digitize_batch_size_for_plan({"openrouter": [], "anthropic": ["claude-opus-5"], "gemini": [], "openai": []}) == 4
    assert digitize_batch_size_for_plan({"openrouter": [], "anthropic": [], "gemini": [], "openai": ["gpt-4o"]}) == 3


def test_digitize_one_by_one_continues_after_failure(monkeypatch):
    calls: list[int] = []

    def fake_one(_db, page, **_kwargs):
        calls.append(page.page_no)
        if page.page_no == 31:
            raise RuntimeError("bad html")
        return f"ok:{page.page_no}"

    monkeypatch.setattr("app.services.pipeline.process_one_page", fake_one)
    db = SimpleNamespace(get=lambda *_a, **_k: None)
    pages = [SimpleNamespace(id=n, page_no=n) for n in (30, 31, 32)]
    notes = digitize_one_by_one(db, pages, force=True)
    assert calls == [30, 31, 32]
    assert notes[0] == "ok:30"
    assert notes[1].startswith("fail:31")
    assert notes[2] == "ok:32"
