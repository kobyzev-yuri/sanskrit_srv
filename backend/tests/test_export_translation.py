"""Translation project exports Word (docx); PDF builds without Chromium."""
import uuid
from pathlib import Path

from docx import Document

HTML = """
<article class="page-style" lang="ru">
  <p class="sa shloka" lang="sa">यज्ञो वै श्रेष्ठतमं कर्म</p>
  <p class="ru tr" lang="ru">Жертва — наилучшее деяние</p>
</article>
"""


def _storage(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()


def test_translation_docx_has_sa_and_ru(monkeypatch, tmp_path: Path):
    _storage(monkeypatch, tmp_path)
    from app.services.export_docx import build_project_docx

    pid = uuid.uuid4()
    path = build_project_docx(
        pid,
        "book-ru",
        "Mantra Pushpam",
        [(1, HTML, None)],
        title_sa="मन्त्रपुष्पम्",
    )
    assert path.suffix == ".docx"
    assert path.is_file() and path.stat().st_size > 1000
    texts = [p.text for p in Document(path.as_posix()).paragraphs]
    blob = "\n".join(texts)
    assert "यज्ञो वै श्रेष्ठतमं कर्म" in blob
    assert "Жертва — наилучшее деяние" in blob


def test_translation_pdf_story_without_chromium(monkeypatch, tmp_path: Path):
    _storage(monkeypatch, tmp_path)
    monkeypatch.setenv("SANSKRIT_PDF_CHROMIUM", "0")
    monkeypatch.setenv("SANSKRIT_PDF_COPYFIX", "0")
    from app.services.export_pdf import build_project_pdf, _chrome_bin, _use_chromium

    assert _use_chromium() is False
    assert _chrome_bin() is None
    pid = uuid.uuid4()
    path = build_project_pdf(
        pid,
        "book-ru",
        "Mantra Pushpam",
        [(1, HTML, None)],
        title_sa="मन्त्रपुष्पम्",
    )
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(data) > 500


def test_bind_images_uses_source_project_figs(monkeypatch, tmp_path: Path):
    _storage(monkeypatch, tmp_path)
    from PIL import Image

    from app.services.export_pdf import _bind_images
    from app.services.layout_assets import figures_dir

    src_pid = uuid.uuid4()
    tr_pid = uuid.uuid4()
    png = figures_dir(src_pid, 3) / "crop-01.png"
    Image.new("RGB", (24, 24), "red").save(png)
    html = (
        f'<figure class="page-figure">'
        f'<img src="/api/v1/pages/{uuid.uuid4()}/figures/crop-01.png" alt="illustration" />'
        f"</figure>"
    )
    bound = _bind_images(html, tr_pid, 3, source_project_id=src_pid)
    assert "data:image/png;base64," in bound
    assert "/api/v1/pages/" not in bound
    assert "[image]" not in bound


def test_story_pdf_embeds_figure(monkeypatch, tmp_path: Path):
    _storage(monkeypatch, tmp_path)
    monkeypatch.setenv("SANSKRIT_PDF_CHROMIUM", "0")
    monkeypatch.setenv("SANSKRIT_PDF_COPYFIX", "0")
    import fitz
    from PIL import Image

    from app.services.export_pdf import build_project_pdf
    from app.services.layout_assets import figures_dir

    src_pid = uuid.uuid4()
    tr_pid = uuid.uuid4()
    png = figures_dir(src_pid, 1) / "crop-01.png"
    Image.new("RGB", (48, 48), "red").save(png)
    html = (
        "<article class='page-style'>"
        f'<figure class="page-figure">'
        f'<img src="/api/v1/pages/{uuid.uuid4()}/figures/crop-01.png" alt="illustration" />'
        f"</figure>"
        "<p class='ru'>подпись</p>"
        "</article>"
    )
    path = build_project_pdf(
        tr_pid,
        "book-ru",
        "Mantra Pushpam",
        [(1, html, None)],
        source_project_id=src_pid,
    )
    doc = fitz.open(path.as_posix())
    try:
        images = [img for i in range(doc.page_count) for img in doc[i].get_images()]
        blob = "".join(page.get_text() for page in doc)
    finally:
        doc.close()
    assert images, "figure PNG must be embedded, not left as [image]"
    assert "[image]" not in blob.lower()

