"""Auto-agree nonempty translations in open_only mode."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Page, PageStatus, Project, Role, User
from app.services.pipeline import (
    agree_nonempty_translations,
    process_one_translate_page,
    translate_accept_status,
)

GOOD = (
    '<article class="page-style" lang="ru">'
    '<p class="sa shloka" lang="sa">यज्ञः</p>'
    '<p class="ru tr" lang="ru">Жертва есть наилучшее деяние</p>'
    "</article>"
)
BLANK = '<article class="page-style" lang="ru">\n</article>'
GARBAGE = (
    '<article class="page-style" lang="ru">'
    "<p>Let me analyze the source HTML and produce a Russian translation</p>"
    "</article>"
)
SRC = '<article class="page-style" lang="sa"><p class="sa">यज्ञः</p></article>'


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def _project(db):
    user = User(
        email="a@test",
        login="admin",
        password_hash="x",
        display_name="A",
        role=Role.admin,
    )
    db.add(user)
    db.flush()
    project = Project(
        slug="book-ru",
        title="Book RU",
        settings={"task": "translate", "translation": {"agreed": True, "style": "interlinear"}},
        created_by=user.id,
    )
    db.add(project)
    db.flush()
    return project


def test_translate_accept_status():
    assert translate_accept_status(auto_agree=True) == PageStatus.expert_done
    assert translate_accept_status(auto_agree=False) == PageStatus.expert_review


def test_agree_nonempty_skips_blank_and_garbage():
    db = _session()
    project = _project(db)
    good = Page(
        project_id=project.id,
        page_no=1,
        status=PageStatus.expert_review,
        source_html=SRC,
        current_html=GOOD,
    )
    blank = Page(
        project_id=project.id,
        page_no=2,
        status=PageStatus.expert_review,
        source_html="<article></article>",
        current_html=BLANK,
    )
    junk = Page(
        project_id=project.id,
        page_no=3,
        status=PageStatus.llm_draft,
        source_html=SRC,
        current_html=GARBAGE,
    )
    empty = Page(
        project_id=project.id,
        page_no=4,
        status=PageStatus.pending,
        source_html=SRC,
        current_html=None,
    )
    db.add_all([good, blank, junk, empty])
    db.commit()
    marked = agree_nonempty_translations(db, project)
    assert marked == [1]
    db.refresh(good)
    db.refresh(blank)
    db.refresh(junk)
    db.refresh(empty)
    assert good.status == PageStatus.expert_done
    assert blank.status == PageStatus.expert_review
    assert junk.status == PageStatus.llm_draft
    assert empty.status == PageStatus.pending


def test_process_one_auto_agree(monkeypatch):
    db = _session()
    project = _project(db)
    page = Page(
        project_id=project.id,
        page_no=5,
        status=PageStatus.pending,
        source_html=SRC,
        current_html=None,
    )
    db.add(page)
    db.commit()

    monkeypatch.setattr(
        "app.services.pipeline.translate_from_source",
        lambda **_k: (GOOD, "gemini:gemini-3.5-flash", {"network": "gemini", "total_tokens": 10}),
    )
    monkeypatch.setattr("app.services.pipeline.record_usage", lambda **_k: None)

    process_one_translate_page(db, page, auto_agree=True)
    db.refresh(page)
    assert page.status == PageStatus.expert_done
    assert "Жертва" in (page.current_html or "")


def test_process_one_without_auto_agree_stays_review(monkeypatch):
    db = _session()
    project = _project(db)
    page = Page(
        project_id=project.id,
        page_no=6,
        status=PageStatus.pending,
        source_html=SRC,
        current_html=None,
    )
    db.add(page)
    db.commit()
    monkeypatch.setattr(
        "app.services.pipeline.translate_from_source",
        lambda **_k: (GOOD, "gemini:gemini-3.5-flash", {"network": "gemini"}),
    )
    monkeypatch.setattr("app.services.pipeline.record_usage", lambda **_k: None)

    process_one_translate_page(db, page, auto_agree=False)
    db.refresh(page)
    assert page.status == PageStatus.expert_review
