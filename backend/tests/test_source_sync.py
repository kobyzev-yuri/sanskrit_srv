"""Copy Sanskrit HTML from a translate page back to digitize with versions."""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, Page, PageStatus, PageVersion, Project, Role, User, VersionSource
from app.services.source_sync import sync_sanskrit_to_digitize


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def _user(db: Session) -> User:
    user = User(
        email="e@test",
        login="expert",
        password_hash="x",
        display_name="Expert",
        role=Role.expert,
    )
    db.add(user)
    db.flush()
    return user


def _pair(db: Session, *, html="OLD", status=PageStatus.expert_done):
    user = _user(db)
    src = Project(slug="book", title="Book", settings={"task": "digitize"}, created_by=user.id)
    tr = Project(
        slug="book-ru",
        title="Book RU",
        settings={"task": "translate"},
        created_by=user.id,
    )
    db.add_all([src, tr])
    db.flush()
    tr.settings = {**tr.settings, "source_project_id": str(src.id)}
    src_page = Page(
        project_id=src.id,
        page_no=3,
        status=status,
        current_html=html,
    )
    tr_page = Page(
        project_id=tr.id,
        page_no=3,
        status=PageStatus.expert_review,
        source_html=html,
        current_html="<p>ru</p>",
    )
    db.add_all([src_page, tr_page])
    db.flush()
    return user, src, tr, src_page, tr_page


def test_sync_snapshots_then_writes_new_version():
    db = _session()
    user, _src, tr, src_page, tr_page = _pair(db)
    assert sync_sanskrit_to_digitize(
        db,
        translate_project=tr,
        translate_page=tr_page,
        html="NEW",
        user=user,
        reason="manual edit",
    )
    db.commit()
    page = db.get(Page, src_page.id)
    assert page.current_html == "NEW"
    assert page.status == PageStatus.expert_review
    vers = list(
        db.scalars(
            select(PageVersion).where(PageVersion.page_id == src_page.id).order_by(PageVersion.version)
        ).all()
    )
    assert [v.html for v in vers] == ["OLD", "NEW"]
    assert vers[0].note == "snapshot before translation sync"
    assert "from translation book-ru p.3" in (vers[1].note or "")
    assert "manual edit" in (vers[1].note or "")
    assert vers[1].source == VersionSource.expert


def test_sync_noop_if_html_unchanged():
    db = _session()
    user, _src, tr, src_page, tr_page = _pair(db, html="SAME", status=PageStatus.expert_review)
    assert not sync_sanskrit_to_digitize(
        db,
        translate_project=tr,
        translate_page=tr_page,
        html="SAME",
        user=user,
        reason="manual edit",
    )
    assert db.scalar(select(PageVersion).where(PageVersion.page_id == src_page.id)) is None
    assert db.get(Page, src_page.id).status == PageStatus.expert_review


def test_sync_skips_when_no_source_project():
    db = _session()
    user = _user(db)
    tr = Project(slug="orphan-ru", title="Orphan", settings={"task": "translate"}, created_by=user.id)
    db.add(tr)
    db.flush()
    page = Page(project_id=tr.id, page_no=1, source_html="<p>sa</p>")
    db.add(page)
    db.flush()
    assert not sync_sanskrit_to_digitize(
        db,
        translate_project=tr,
        translate_page=page,
        html="<p>changed</p>",
        user=user,
        reason="manual edit",
    )


def test_sync_skips_empty_html():
    db = _session()
    user, _src, tr, src_page, _tr_page = _pair(db)
    assert not sync_sanskrit_to_digitize(
        db,
        translate_project=tr,
        translate_page=_tr_page,
        html="  ",
        user=user,
        reason="manual edit",
    )
    assert db.get(Page, src_page.id).current_html == "OLD"
    assert db.get(Page, src_page.id).status == PageStatus.expert_done


def test_sync_does_not_follow_wrong_page_no():
    db = _session()
    user, _src, tr, src_page, tr_page = _pair(db)
    tr_page.page_no = 99
    db.flush()
    assert not sync_sanskrit_to_digitize(
        db,
        translate_project=tr,
        translate_page=tr_page,
        html="NEW",
        user=user,
        reason="manual edit",
    )
    assert db.get(Page, src_page.id).current_html == "OLD"
