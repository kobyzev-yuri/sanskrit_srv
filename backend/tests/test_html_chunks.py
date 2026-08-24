"""HTML chunk split/merge for large-page translation."""
from app.services.html_chunks import (
    chunk_page_html,
    merge_translated_chunks,
    pack_blocks,
    split_top_level_blocks,
    unwrap_article,
)


def test_unwrap_article():
    html = '<article class="page-style" lang="sa">\n<p>a</p>\n</article>'
    open_tag, inner, close = unwrap_article(html)
    assert open_tag.startswith("<article")
    assert "<p>a</p>" in inner
    assert close == "</article>"


def test_split_top_level_blocks():
    inner = '<p class="sa">एक</p>\n<figure><img src="/x.png"/></figure>\n<p>दो</p>'
    blocks = split_top_level_blocks(inner)
    assert len(blocks) == 3
    assert blocks[0].startswith("<p")
    assert "<figure" in blocks[1]
    assert "दो" in blocks[2]


def test_pack_blocks_respects_soft():
    blocks = [f"<p>{'x' * 100}</p>" for _ in range(20)]
    packed = pack_blocks(blocks, soft=400, hard=800)
    assert len(packed) > 1
    assert all(len(c) <= 800 + 50 for c in packed)  # one block may slightly exceed soft


def test_chunk_page_html_small_stays_one():
    html = '<article class="page-style"><p>short</p></article>'
    assert chunk_page_html(html) == [html]


def test_chunk_page_html_large_splits():
    paras = "".join(f"<p class='sa'>{'श' * 200} {i}</p>\n" for i in range(50))
    html = f'<article class="page-style" lang="sa">\n{paras}</article>'
    chunks = chunk_page_html(html, soft=1500, hard=2500, min_split=2000)
    assert len(chunks) > 1
    for c in chunks:
        assert "<article" in c.lower()
        assert "</article>" in c.lower()


def test_merge_translated_chunks():
    parts = [
        '<article class="page-style" lang="ru"><p class="ru">один</p></article>',
        '<article lang="ru"><p class="ru">два</p></article>',
    ]
    merged = merge_translated_chunks(parts, article_open='<article class="page-style" lang="sa">')
    assert merged.count("<article") == 1
    assert "один" in merged and "два" in merged
    assert 'lang="ru"' in merged.lower()
