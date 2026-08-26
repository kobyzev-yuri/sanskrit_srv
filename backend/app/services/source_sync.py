"""Copy Sanskrit HTML from a translate page back onto the linked digitize page.

The digitize `current_html` is replaced only after the previous text is stored
as a PageVersion, so a bad sync can be rolled back from history.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Page, PageStatus, PageVersion, Project, Role, User, VersionSource
from app.services.translation_style import project_task


def linked_digitize_page(db: Session, translate_project: Project | None, page_no: int) -> Page | None:
    if translate_project is None or project_task(translate_project) != "translate":
        return None
    raw = (translate_project.settings or {}).get("source_project_id")
    if not raw:
        return None
    try:
        pid = uuid.UUID(str(raw))
    except ValueError:
        return None
    src = db.get(Project, pid)
    if src is None or project_task(src) == "translate":
        return None
    return db.scalar(select(Page).where(Page.project_id == pid, Page.page_no == int(page_no)))


def _version_source(user: User | None) -> VersionSource:
    if user is not None and getattr(user, "role", None) == Role.scholar:
        return VersionSource.scholar
    return VersionSource.expert


def _next_ver(db: Session, page_id) -> int:
    return (db.scalar(select(func.max(PageVersion.version)).where(PageVersion.page_id == page_id)) or 0) + 1


def _add_version(
    db: Session,
    page: Page,
    user: User | None,
    html: str,
    note: str,
) -> None:
    db.add(
        PageVersion(
            page_id=page.id,
            version=_next_ver(db, page.id),
            html=html,
            source=_version_source(user),
            created_by=getattr(user, "id", None),
            note=(note or "")[:500] or None,
        )
    )
    db.flush()


def _snapshot_if_needed(db: Session, page: Page, user: User | None) -> None:
    current = page.current_html or ""
    if not current.strip():
        return
    last = db.scalar(
        select(PageVersion)
        .where(PageVersion.page_id == page.id)
        .order_by(PageVersion.version.desc())
        .limit(1)
    )
    if last is not None and last.html == current:
        return
    _add_version(db, page, user, current, "snapshot before translation sync")


def sync_sanskrit_to_digitize(
    db: Session,
    *,
    translate_project: Project | None,
    translate_page: Page,
    html: str,
    user: User | None,
    reason: str,
) -> bool:
    """Write `html` onto the linked digitize page as a new version.

    Returns True if the digitize page was updated. No-op when HTML is unchanged,
    empty, or the translate project has no digitize source.
    An already-accepted digitize page is returned to expert_review.
    """
    incoming = html or ""
    if not incoming.strip():
        return False
    dest = linked_digitize_page(db, translate_project, translate_page.page_no)
    if dest is None:
        return False
    if (dest.current_html or "") == incoming:
        return False

    _snapshot_if_needed(db, dest, user)
    dest.current_html = incoming
    if dest.status == PageStatus.expert_done:
        dest.status = PageStatus.expert_review
    elif dest.status in (PageStatus.pending, PageStatus.llm_draft, PageStatus.ocr, PageStatus.extracting):
        dest.status = PageStatus.expert_review

    slug = getattr(translate_project, "slug", None) or "translate"
    note = f"from translation {slug} p.{translate_page.page_no} | {reason}"
    _add_version(db, dest, user, incoming, note)
    return True
