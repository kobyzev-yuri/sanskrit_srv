from types import SimpleNamespace
from uuid import uuid4

from app.services.draft_search import html_to_text, iter_matches, search_pages


def test_html_to_text_strips_tags():
    html = '<article class="page-style"><p class="sa">गणपतिग्ं</p><p class="ru">Ганапати</p></article>'
    text = html_to_text(html)
    assert "गणपतिग्ं" in text
    assert "Ганапати" in text
    assert "<p" not in text


def test_iter_matches_devanagari_and_latin():
    text = "foo गणपतिग्ं bar GANAPATI"
    assert iter_matches(text, "गणपतिग्ं")
    assert iter_matches(text, "ganapati")  # casefold
    assert not iter_matches(text, "неттакого")


def test_search_pages_draft_and_source():
    pages = [
        SimpleNamespace(
            id=uuid4(),
            page_no=1,
            current_html="<p>hello गणपति</p>",
            source_html="<p>ignored</p>",
        ),
        SimpleNamespace(
            id=uuid4(),
            page_no=2,
            current_html="<p>nothing</p>",
            source_html="<p>गणपति here</p>",
        ),
        SimpleNamespace(
            id=uuid4(),
            page_no=3,
            current_html=None,
            source_html=None,
        ),
    ]
    draft_only = search_pages(pages, "गणपति", include_source=False)
    assert draft_only["page_hits"] == 1
    assert draft_only["hits"][0]["page_no"] == 1
    both = search_pages(pages, "गणपति", include_source=True)
    assert both["page_hits"] == 2
    assert {h["page_no"] for h in both["hits"]} == {1, 2}
