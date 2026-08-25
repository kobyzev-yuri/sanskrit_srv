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
    from app.services.export_pdf import build_project_pdf, _chrome_bin

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
