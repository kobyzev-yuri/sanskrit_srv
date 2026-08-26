"""Proofread JSON parse + apply (digitize and translation)."""
from app.services.llm_proofread import (
    _extract_json,
    _normalize_suggestions,
    apply_proofread_suggestions,
    gross_draft_items,
    remaining_after_apply,
    split_by_target,
)


SA = '<p class="sa shloka" lang="sa">यज्ञो वै श्रेष्ठतमं कर्म</p>'
DRAFT = f"<article>{SA}</article>"
COMPLETE = f'<article>{SA}<p class="ru tr" lang="ru">Жертва — наилучшее деяние</p></article>'


def test_extract_json_from_fence():
    raw = 'note\n```json\n{"suggestions":[]}\n```\n'
    assert _extract_json(raw) == {"suggestions": []}


def test_extract_json_outer_object():
    raw = 'thinking {"suggestions":[{"id":"1","wrong":"а","right":"б"}]} trailing'
    data = _extract_json(raw)
    assert data["suggestions"][0]["right"] == "б"


def test_skip_wrong_not_in_html():
    items = _normalize_suggestions(
        [{"wrong": "неттакого", "right": "есть", "severity": "high"}],
        draft_html=DRAFT,
    )
    assert items == []


def test_source_only_target():
    src = "<p>गणपतिग्ं</p>"
    items = _normalize_suggestions(
        [
            {
                "wrong": "गणपतिग्ं",
                "right": "गणपतिग्ं",  # same — skipped
            },
            {
                "wrong": "गणपतिग्ं",
                "right": "गणपतिं",
                "severity": "low",
                "kind": "sanskrit",
                "target": "source",
            },
        ],
        draft_html=DRAFT,
        source_html=src,
    )
    # first skipped (same), second kept even though not in draft
    assert len(items) == 1
    assert items[0]["target"] == "source"
    assert items[0]["kind"] == "sanskrit"


def test_retarget_to_source_if_only_there():
    items = _normalize_suggestions(
        [{"wrong": "ऋतग्ं", "right": "ऋतं", "target": "draft"}],
        draft_html=DRAFT,
        source_html="<p>ऋतग्ं</p>",
    )
    assert items[0]["target"] == "source"


def test_apply_incomplete_shloka():
    right = SA + '<p class="ru tr" lang="ru">Жертва — наилучшее деяние</p>'
    html, applied = apply_proofread_suggestions(
        DRAFT,
        [{"id": "1", "wrong": SA, "right": right, "severity": "high", "kind": "incomplete"}],
    )
    assert "Жертва" in html
    assert html.count("यज्ञो") == 1
    assert len(applied) == 1


def test_apply_longest_first():
    html = "abcdef"
    out, applied = apply_proofread_suggestions(
        html,
        [
            {"wrong": "abc", "right": "X"},
            {"wrong": "abcdef", "right": "Y"},
        ],
    )
    assert out == "Y"
    assert [a["right"] for a in applied] == ["Y"]


def test_gross_skips_sanskrit():
    items = [
        {"severity": "high", "kind": "incomplete", "target": "draft", "wrong": "a", "right": "b"},
        {"severity": "high", "kind": "sanskrit", "target": "both", "wrong": "c", "right": "d"},
        {"severity": "medium", "kind": "sense", "target": "draft", "wrong": "e", "right": "f"},
    ]
    gross = gross_draft_items(items)
    assert len(gross) == 1
    assert gross[0]["kind"] == "incomplete"


def test_split_and_remaining():
    accepted = [
        {"wrong": "a", "right": "A", "target": "draft"},
        {"wrong": "b", "right": "B", "target": "source"},
        {"wrong": "c", "right": "C", "target": "both"},
    ]
    draft, source = split_by_target(accepted)
    assert len(draft) == 2
    assert len(source) == 2
    leftover = remaining_after_apply(accepted, [{"wrong": "a", "right": "A", "target": "draft"}])
    assert [x["wrong"] for x in leftover] == ["b", "c"]


def test_join_replace_on_page_start():
    html = '<p class="ru tr">и продолжение фразы</p>'
    items = _normalize_suggestions(
        [
            {
                "wrong": "и продолжение фразы",
                "right": "— и продолжение фразы с предыдущей страницы",
                "kind": "join",
                "severity": "high",
                "target": "draft",
            }
        ],
        draft_html=html,
    )
    out, applied = apply_proofread_suggestions(html, items)
    assert "предыдущей" in out
    assert applied
