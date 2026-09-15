"""Auto-agree nonempty digitize drafts in open_only mode."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Page, PageStatus, Project, Role, User
from app.services.pipeline import agree_nonempty_digitize_drafts

GOOD = (
    '<article class="page-style" lang="sa">'
    '<p class="sa">यज्ञो वै श्रेष्ठतमं कर्म तस्माद्यज्ञे सर्वं प्रतिष्ठितम्</p>'
    "</article>"
)
SHORT = '<article class="page-style" lang="sa"></article>'
GARBAGE = (
    '<article class="page-style" lang="sa">'
    "<p>Let me analyze the source HTML and produce a page draft</p>"
    "</article>"
)


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
        slug="book-sa",
        title="Book SA",
        settings={"task": "digitize", "source_kind": "scan"},
        created_by=user.id,
    )
    db.add(project)
    db.flush()
    return project


def test_agree_nonempty_digitize_skips_short_and_garbage():
    db = _session()
    project = _project(db)
    good = Page(
        project_id=project.id,
        page_no=1,
        status=PageStatus.expert_review,
        current_html=GOOD,
    )
    short = Page(
        project_id=project.id,
        page_no=2,
        status=PageStatus.expert_review,
        current_html=SHORT,
    )
    junk = Page(
        project_id=project.id,
        page_no=3,
        status=PageStatus.llm_draft,
        current_html=GARBAGE,
    )
    empty = Page(
        project_id=project.id,
        page_no=4,
        status=PageStatus.pending,
        current_html=None,
    )
    already = Page(
        project_id=project.id,
        page_no=5,
        status=PageStatus.expert_done,
        current_html=GOOD,
    )
    db.add_all([good, short, junk, empty, already])
    db.commit()
    marked = agree_nonempty_digitize_drafts(db, project)
    assert marked == [1]
    db.refresh(good)
    db.refresh(short)
    db.refresh(junk)
    db.refresh(empty)
    db.refresh(already)
    assert good.status == PageStatus.expert_done
    assert short.status == PageStatus.expert_review
    assert junk.status == PageStatus.llm_draft
    assert empty.status == PageStatus.pending
    assert already.status == PageStatus.expert_done
