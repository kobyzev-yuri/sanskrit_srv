import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_roles
from app.models import Page, PageStatus, PageVersion, Project, Role, User, VersionSource
from app.schemas import (
    PageDetailOut,
    PageHtmlIn,
    PageOut,
    PageReviewAgainIn,
    PageReviseIn,
    PageVersionOut,
)
from app.services.directive_fix import apply_directive_replacements
from app.services.layout_assets import extract_embedded_figures, finalize_page_html, figure_file
from app.services.llm_draft import revise_from_scan
from app.services.llm_status import LlmQuotaError
from app.services.llm_usage import record_usage
from app.services.pipeline import DEFAULT_REVIEW_DIRECTIVE

router = APIRouter(tags=["pages"])


def _uid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found") from exc


def _page_out(page: Page) -> PageOut:
    return PageOut(
        id=page.id,
        project_id=page.project_id,
        page_no=page.page_no,
        status=page.status,
        has_scan=bool(page.scan_path and Path(page.scan_path).exists()),
        has_html=bool(page.current_html),
        updated_at=page.updated_at,
    )


@router.get("/projects/{project_id}/pages", response_model=list[PageOut])
def list_pages(
    project_id: str,
    status_filter: PageStatus | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pid = _uid(project_id)
    q = select(Page).where(Page.project_id == pid).order_by(Page.page_no)
    if status_filter:
        q = q.where(Page.status == status_filter)
    return [_page_out(p) for p in db.scalars(q).all()]


@router.get("/pages/{page_id}", response_model=PageDetailOut)
def get_page(page_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    scan_url = f"/api/v1/pages/{page.id}/scan" if page.scan_path else None
    return PageDetailOut(
        id=page.id,
        project_id=page.project_id,
        page_no=page.page_no,
        status=page.status,
        current_html=page.current_html,
        scan_url=scan_url,
        updated_at=page.updated_at,
    )


@router.get("/pages/{page_id}/scan")
def get_scan(page_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    page = db.get(Page, _uid(page_id))
    if page is None or not page.scan_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scan not found")
    path = Path(page.scan_path)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scan file missing")
    return FileResponse(path, media_type="image/png")


@router.get("/pages/{page_id}/figures/{name}")
def get_figure(
    page_id: str,
    name: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    path = figure_file(page.project_id, page.page_no, name)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Figure not found")
    return FileResponse(path, media_type="image/png")


@router.patch("/pages/{page_id}", response_model=PageDetailOut)
def save_html(
    page_id: str,
    body: PageHtmlIn,
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    if user.role == Role.reader:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Readers cannot edit")

    page.current_html = body.html
    if page.status in (PageStatus.pending, PageStatus.llm_draft, PageStatus.ocr):
        page.status = PageStatus.expert_review

    next_ver = (
        db.scalar(select(func.max(PageVersion.version)).where(PageVersion.page_id == page.id)) or 0
    ) + 1
    source = VersionSource.expert if user.role in (Role.admin, Role.expert) else VersionSource.scholar
    db.add(
        PageVersion(
            page_id=page.id,
            version=next_ver,
            html=body.html,
            source=source,
            created_by=user.id,
            note=body.note,
        )
    )
    db.commit()
    db.refresh(page)
    return get_page(str(page.id), user, db)


@router.post("/pages/{page_id}/accept", response_model=PageDetailOut)
@router.post("/pages/{page_id}/submit-expert", response_model=PageDetailOut)
def accept_page(
    page_id: str,
    user: User = Depends(require_roles(Role.admin, Role.expert)),
    db: Session = Depends(get_db),
):
    """Expert accepts current draft for this page."""
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    if not page.current_html:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Nothing to accept — wait for draft")
    page.status = PageStatus.expert_done
    page.assigned_expert_id = user.id
    db.commit()
    db.refresh(page)
    return get_page(str(page.id), user, db)


@router.post("/pages/{page_id}/revoke", response_model=PageDetailOut)
def revoke_page(
    page_id: str,
    user: User = Depends(require_roles(Role.admin, Role.expert)),
    db: Session = Depends(get_db),
):
    """Revoke acceptance → page back to expert review / editing."""
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    page.status = PageStatus.expert_review
    db.commit()
    db.refresh(page)
    return get_page(str(page.id), user, db)


def _apply_llm_revision(
    db: Session,
    page: Page,
    user: User,
    directive: str,
) -> Page:
    if not page.scan_path or not Path(page.scan_path).exists():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Page has no scan yet — pipeline still running")

    # Fast path: exact «получилось X исправь на Y» / «исправь X на Y» in current HTML.
    base_html = page.current_html or ""
    replaced_html, applied = apply_directive_replacements(base_html, directive)
    if applied:
        html = finalize_page_html(
            replaced_html,
            scan_path=Path(page.scan_path),
            project_id=page.project_id,
            page_no=page.page_no,
            page_id=page.id,
        )
        page.current_html = html
        page.status = PageStatus.expert_review
        next_ver = (
            db.scalar(select(func.max(PageVersion.version)).where(PageVersion.page_id == page.id)) or 0
        ) + 1
        note = "directive-replace | " + "; ".join(f"{a}→{b}" for a, b in applied)
        if directive:
            note = f"{note} | {directive[:400]}"
        db.add(
            PageVersion(
                page_id=page.id,
                version=next_ver,
                html=html,
                source=VersionSource.expert,
                created_by=user.id,
                note=note,
            )
        )
        db.commit()
        db.refresh(page)
        return page

    project = db.get(Project, page.project_id)
    figs: list[dict] = []
    if project and project.source_pdf_path:
        try:
            figs = extract_embedded_figures(Path(project.source_pdf_path), page.project_id, page.page_no)
        except Exception:  # noqa: BLE001
            figs = []
    try:
        html, model, usage = revise_from_scan(
            Path(page.scan_path),
            page_no=page.page_no,
            current_html=page.current_html,
            directive=directive,
            available_figures=figs or None,
        )
    except LlmQuotaError as exc:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": "llm_quota", "message": str(exc)},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"LLM revise failed: {exc}") from exc

    # Apply quoted replacements again in case the model missed a tiny fix.
    html, _ = apply_directive_replacements(html, directive)

    html = finalize_page_html(
        html,
        scan_path=Path(page.scan_path),
        project_id=page.project_id,
        page_no=page.page_no,
        page_id=page.id,
    )

    record_usage(
        db,
        project_id=page.project_id,
        page_id=page.id,
        network=str(usage.get("network") or "gemini"),
        model=str(usage.get("model") or model.split(":", 1)[-1]),
        usage=usage,
        operation="revise",
    )

    page.current_html = html
    page.status = PageStatus.expert_review
    next_ver = (
        db.scalar(select(func.max(PageVersion.version)).where(PageVersion.page_id == page.id)) or 0
    ) + 1
    db.add(
        PageVersion(
            page_id=page.id,
            version=next_ver,
            html=html,
            source=VersionSource.llm,
            created_by=user.id,
            note=f"{model} | {directive[:500]}",
        )
    )
    db.commit()
    db.refresh(page)
    return page


@router.post("/pages/{page_id}/revise", response_model=PageDetailOut)
def revise_page(
    page_id: str,
    body: PageReviseIn,
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    """Expert task: what is wrong — LLM re-drafts from scan + directive."""
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    page = _apply_llm_revision(db, page, user, body.directive)
    return get_page(str(page.id), user, db)


@router.post("/pages/{page_id}/review-again", response_model=PageDetailOut)
def review_again(
    page_id: str,
    body: PageReviewAgainIn = PageReviewAgainIn(),
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    """Shortcut: «пересмотри страницу» (optional custom directive)."""
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    directive = body.directive or DEFAULT_REVIEW_DIRECTIVE
    page = _apply_llm_revision(db, page, user, directive)
    return get_page(str(page.id), user, db)


@router.get("/pages/{page_id}/versions", response_model=list[PageVersionOut])
def list_versions(
    page_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pid = _uid(page_id)
    rows = db.scalars(
        select(PageVersion).where(PageVersion.page_id == pid).order_by(PageVersion.version.desc())
    ).all()
    return list(rows)
