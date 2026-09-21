"""IAST reverse correction onto Devanagari HTML."""
from app.services.iast_correct import (
    apply_iast_corrections,
    overlay_sa_onto_source,
    source_for_digitize,
)
from app.services.iast_script import deo_to_iast, iast_to_deva, norm_iast


BLOCK = """
<article class="page-style" lang="sa">
  <p class="sa shloka" lang="sa">यज्ञो वै श्रेष्ठतमं कर्म</p>
  <p class="iast" lang="sa-Latn">yajño vai śreṣṭhatamaṃ karmaḥ</p>
  <p class="sa shloka" lang="sa">शिवः शिवः</p>
  <p class="iast" lang="sa-Latn">śivaḥ śivaḥ</p>
</article>
"""

SOURCE = """
<article class="page-style" lang="sa">
  <p class="sa shloka" lang="sa">यज्ञो वै श्रेष्ठतमं कर्म</p>
  <p class="sa shloka" lang="sa">शिवः शिवः</p>
</article>
"""

PLAIN = """
<article class="page-style" lang="sa-Latn">
  <p class="iast" lang="sa-Latn">yajño vai śreṣṭhatamaṃ karmaḥ</p>
  <p class="iast" lang="sa-Latn">śivaḥ śivaḥ</p>
</article>
"""


def test_roundtrip_common_verses():
    samples = [
        "यज्ञो वै श्रेष्ठतमं कर्म",
        "कृष्णोऽस्मि",
        "ॐ तत् सत्",
        "गणपतिग्ं",
        "ऋतं",
        "१. अग्निः",
        "ज्ञानम्",
        "क्षत्रियः",
        "वाक्",
        "तेऽपि",
        "हंसाः",
    ]
    for deva in samples:
        iast = deo_to_iast(deva)
        back = iast_to_deva(iast, old_deva=deva)
        assert back == deva, (deva, iast, back)


def test_visarga_edit_roundtrips():
    assert iast_to_deva("karmaḥ") == "कर्मः"
    assert "ḥ" in deo_to_iast("कर्मः")


def test_apply_updates_only_edited_iast_pair():
    result = apply_iast_corrections(BLOCK, SOURCE)
    assert result.changed == 1
    assert result.changes[0].old == "यज्ञो वै श्रेष्ठतमं कर्म"
    assert result.changes[0].new == "यज्ञो वै श्रेष्ठतमं कर्मः"
    assert "यज्ञो वै श्रेष्ठतमं कर्मः" in result.draft_html
    assert "शिवः शिवः" in result.draft_html
    assert "यज्ञो वै श्रेष्ठतमं कर्मः" in result.source_html
    assert "शिवः शिवः" in result.source_html


def test_apply_noop_when_iast_matches_deva():
    html = BLOCK.replace("karmaḥ", "karma")
    result = apply_iast_corrections(html, SOURCE)
    assert result.changed == 0
    assert "कर्मः" not in result.draft_html


def test_apply_plain_updates_source_only():
    result = apply_iast_corrections(PLAIN, SOURCE)
    assert result.changed == 1
    assert "कर्मः" in result.source_html
    assert 'class="sa"' not in result.draft_html or "कर्मः" not in result.draft_html.split("iast")[0]
    assert "yajño vai śreṣṭhatamaṃ karmaḥ" in result.draft_html


def test_skip_english_note_tagged_as_iast():
    html = """
    <article>
      <p class="sa">अध्यायः</p>
      <p class="iast">Chapter Two of the book</p>
    </article>
    """
    result = apply_iast_corrections(html, '<p class="sa">अध्यायः</p>')
    assert result.changed == 0


def test_overlay_sa_from_draft():
    draft = """
    <article class="page-style" lang="sa">
      <p class="sa shloka" lang="sa">यज्ञो वै श्रेष्ठतमं कर्मः</p>
      <p class="iast" lang="sa-Latn">yajño vai śreṣṭhatamaṃ karmaḥ</p>
      <p class="sa shloka" lang="sa">शिवः शिवः</p>
      <p class="iast" lang="sa-Latn">śivaḥ śivaḥ</p>
    </article>
    """
    out, changes = overlay_sa_onto_source(SOURCE, draft)
    assert len(changes) == 1
    assert "कर्मः" in out
    assert changes[0].new.endswith("कर्मः")


def test_source_for_digitize_prefers_left_when_draft_unchanged():
    left = SOURCE.replace("यज्ञो वै श्रेष्ठतमं कर्म", "यज्ञो वै श्रेष्ठतमं कर्मः")
    html, changes = source_for_digitize(
        saved_source=SOURCE,
        incoming_source=left,
        draft_html=BLOCK.replace("karmaḥ", "karma"),
    )
    assert html == left
    assert changes == []


def test_source_for_digitize_counts_draft_when_left_already_fixed():
    left = SOURCE.replace("यज्ञो वै श्रेष्ठतमं कर्म", "यज्ञो वै श्रेष्ठतमं कर्मः")
    draft = BLOCK.replace(
        '<p class="sa shloka" lang="sa">यज्ञो वै श्रेष्ठतमं कर्म</p>',
        '<p class="sa shloka" lang="sa">यज्ञो वै श्रेष्ठतमं कर्मः</p>',
    )
    html, changes = source_for_digitize(
        saved_source=SOURCE,
        incoming_source=left,
        draft_html=draft,
    )
    assert "कर्मः" in html
    assert len(changes) == 1


def test_live_iast_lines_override_stale_html():
    html = """
    <article class="page-style" lang="sa">
      <p class="sa shloka" lang="sa">स्वैर्स्वच्छस्फुरद्भाभिर्भासिताण्वाद्युपायतः ।</p>
      <p class="iast" lang="sa-Latn">svairsvacchasphuradbhābhirbhāsitāṇvādyupāyataḥ |</p>
    </article>
    """
    gold_iast = "svairasvacchasphuradbhābhirbhāsitāṇavāyupāyataḥ |"
    result = apply_iast_corrections(html, html, iast_lines=[gold_iast])
    assert result.changed == 1
    assert "स्वैरस्वच्छ" in result.draft_html
    assert "णवायुपायतः" in result.draft_html
    assert "स्वैर्स्वच्छ" not in result.draft_html
    assert "ण्वाद्यु" not in result.draft_html


def test_gold_verse_iast_roundtrip():
    gold = "स्वैरस्वच्छस्फुरद्भाभिर्भासिताणवायुपायतः ।"
    iast = deo_to_iast(gold)
    assert iast.startswith("svaira")
    assert "ṇavāyu" in iast
    assert iast_to_deva(iast) == gold


SHLOKA_DRAFT = """
<article class="page-style" lang="sa">
  <div class="shloka sa">
    <p class="narrow">स्वैरस्वच्छस्फुरद्भाभिर्भासिताणवायुपायतः ।</p>
    <p class="iast narrow" lang="sa-Latn">svairisvacchasphuradbhābhirbhāsitāṇvādyupāyataḥ |</p>
    <p class="narrow indent">स्वान्मेषाच्छाम्भवावेशं दर्शयन्तीं शिवां श्रये ॥ २ ॥</p>
    <p class="iast narrow indent" lang="sa-Latn">svānmeṣācchāmbhavāveśaṃ darśayantīṃ śivāṃ śraye || 2 ||</p>
  </div>
</article>
"""

SHLOKA_SOURCE = """
<article class="page-style" lang="sa">
  <div class="shloka sa">
    <p class="narrow">स्वैरस्वच्छस्फुरद्भाभिर्भासिताणवायुपायतः ।</p>
    <p class="narrow indent">स्वान्मेषाच्छाम्भवावेशं दर्शयन्तीं शिवां श्रये ॥ २ ॥</p>
  </div>
</article>
"""


def test_shloka_wrapper_updates_inner_deva_from_iast():
    result = apply_iast_corrections(SHLOKA_DRAFT, SHLOKA_SOURCE)
    assert result.changed == 1
    assert "स्वैरिस्वच्छ" in result.draft_html
    assert "ण्वाद्युपायतः" in result.draft_html
    assert "स्वैरस्वच्छस्फुरद्भाभिर्भासिताणवायुपायतः" not in result.draft_html
    assert "स्वान्मेषाच्छाम्भवावेशं" in result.draft_html
    assert "स्वैरिस्वच्छ" in result.source_html
    assert "svairi" not in result.source_html


def test_skip_unrelated_commentary_iast_after_lemma():
    html = """
    <article>
      <p class="sa indent">भीरूणामभयप्रदो भवभयाक्रन्दस्य हेतुस्ततो</p>
      <p class="iast indent" lang="sa-Latn">iha śrīmān cidbhairavaḥ pūrṇāṃ haṃvimarśātmakaparaśaktisphurattābhittā-</p>
    </article>
    """
    result = apply_iast_corrections(html, html)
    assert result.changed == 0
    assert "भीरूणामभयप्रदो" in result.draft_html
    assert "इह श्रीमान्" not in result.draft_html


def test_overlay_shloka_wrapper_copies_only_deva():
    draft = SHLOKA_DRAFT.replace(
        "स्वैरस्वच्छस्फुरद्भाभिर्भासिताणवायुपायतः ।",
        "स्वैरिस्वच्छस्फुरद्भाभिर्भासिताण्वाद्युपायतः ।",
    )
    out, changes = overlay_sa_onto_source(SHLOKA_SOURCE, draft)
    assert len(changes) == 1
    assert "स्वैरिस्वच्छ" in out
    assert "svairi" not in out
    assert "स्वान्मेषाच्छाम्भवावेशं" in out
