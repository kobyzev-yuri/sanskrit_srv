from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user, require_roles
from app.models import Job, JobStatus, Page, PageStatus, Project, Role, User
from app.schemas import ExtractIn, JobOut, ProjectOut, ProjectSettingsIn, ProjectUsageOut
from app.services import storage
from app.services.export_docx import build_project_docx
from app.services.export_pdf import build_project_pdf
from app.services.llm_usage import project_usage_summary
from app.services.pdf_extract import classify_pdf, extract_pages, pdf_page_count
from app.services.pipeline import enqueue_project_pipeline, ensure_page_stubs

router = APIRouter(prefix="/projects", tags=["projects"])


def _uid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found") from exc


def _default_settings() -> dict:
    s = get_settings()
    from app.services.llm_route import describe_route

    route = describe_route()
    primary = route["primary"]
    fb = route["fallback_models"]
    fallback = []
    if primary.get("provider") == "openrouter":
        fallback = []
    elif primary.get("provider") == "anthropic":
        fallback = [
            {"provider": "gemini", "model": fb["gemini"]},
            {"provider": "openai", "model": fb["openai"]},
        ]
    else:
        fallback = [{"provider": "openai", "model": fb["openai"]}]
    return {
        "llm": {
            "openrouter": {
                "api_key_env": "OPENROUTER_API_KEY",
                "base_url": s.openrouter_base_url,
                "model": s.openrouter_model,
            },
            "proxyapi": {
                "api_key_env": "OPENAI_API_KEY",
                "anthropic_base_url": s.anthropic_base_url,
                "gemini_base_url": s.gemini_base_url,
                "openai_base_url": s.openai_base_url,
            },
            "active_route": route["route"],
            "routes": {
                "lesson": {
                    "primary": primary,
                    "fallback": fallback,
                },
                "alphabet": {
                    "primary": {"provider": "openai", "model": s.openai_model},
                    "fallback": [{"provider": "openai", "model": "gpt-4o"}],
                },
            },
        }
    }


def _project_out(db: Session, project: Project) -> ProjectOut:
    pages = list(db.scalars(select(Page).where(Page.project_id == project.id)).all())
    count = len(pages)
    # expert_review = согласие отозвано / на правке; expert_done = согласовано (по умолчанию после авточерновика)
    ready = sum(1 for p in pages if p.status == PageStatus.expert_review and p.current_html)
    accepted = sum(1 for p in pages if p.status == PageStatus.expert_done)
    pdf_pages = None
    if project.source_pdf_path and Path(project.source_pdf_path).exists():
        try:
            pdf_pages = pdf_page_count(Path(project.source_pdf_path))
        except Exception:
            pdf_pages = None
    job = db.scalar(
        select(Job)
        .where(Job.project_id == project.id)
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    settings = project.settings or {}
    threshold = get_settings().large_book_pages
    pages_n = pdf_pages or count
    awaiting = project.status == "awaiting_confirm"
    large = bool(pages_n and pages_n > threshold)
    confirm_required = awaiting or (
        large and job is None and project.status in ("draft", "awaiting_confirm")
    )
    return ProjectOut(
        id=project.id,
        slug=project.slug,
        title=project.title,
        title_sa=project.title_sa,
        status=project.status,
        settings=settings,
        page_count=count,
        pdf_pages=pdf_pages,
        draft_ready=ready,
        accepted=accepted,
        source_kind=settings.get("source_kind") or "scan",
        confirm_required=confirm_required,
        pipeline=JobOut.model_validate(job) if job else None,
        created_at=project.created_at,
    )


@router.get("", response_model=list[ProjectOut])
def list_projects(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    projects = list(db.scalars(select(Project).order_by(Project.created_at.desc())).all())
    return [_project_out(db, p) for p in projects]


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    slug: str = Form(...),
    title: str = Form(...),
    title_sa: str | None = Form(None),
    file: UploadFile = File(...),
    user: User = Depends(require_roles(Role.admin)),
    db: Session = Depends(get_db),
):
    """Upload PDF → classify. Auto-pipeline if ≤100 pages; else await confirm for whole book."""
    slug = slug.strip().lower()
    if db.scalar(select(Project).where(Project.slug == slug)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Slug already exists")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="PDF required")

    settings = _default_settings()
    project = Project(
        slug=slug,
        title=title,
        title_sa=title_sa,
        status="draft",
        settings=settings,
        created_by=user.id,
    )
    db.add(project)
    db.flush()
    project_id = project.id

    data = await file.read()
    # Disk + PyMuPDF off the event loop so UI polling stays responsive during big uploads.
    try:
        pdf_path, info = await asyncio.to_thread(_save_and_classify, project_id, file.filename, data)
    except Exception as exc:
        storage.remove_project_files(project_id)
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"PDF open failed: {exc}") from exc

    project.source_pdf_path = str(pdf_path)
    settings["source_kind"] = info["kind"]
    settings["source_detect"] = {
        "avg_chars": info["avg_chars"],
        "samples": info["samples"][:5],
    }
    project.settings = settings
    db.commit()
    db.refresh(project)

    try:
        total = ensure_page_stubs(db, project)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"PDF open failed: {exc}") from exc

    threshold = get_settings().large_book_pages
    if total > threshold:
        project.status = "awaiting_confirm"
        db.commit()
        db.refresh(project)
        return _project_out(db, project)

    project.status = "processing"
    db.commit()
    enqueue_project_pipeline(db, project.id, force=False)
    db.refresh(project)
    return _project_out(db, project)


def _save_and_classify(project_id, filename: str, data: bytes):
    pdf_path = storage.save_upload_pdf(project_id, filename, data)
    info = classify_pdf(pdf_path)
    return pdf_path, info


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project = db.get(Project, _uid(project_id))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _project_out(db, project)


@router.get("/{project_id}/usage", response_model=ProjectUsageOut)
def get_project_usage(
    project_id: str,
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    """Token usage by ProxyAPI network/model for project billing."""
    project = db.get(Project, _uid(project_id))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    return ProjectUsageOut.model_validate(project_usage_summary(db, project.id))


@router.post("/{project_id}/pipeline", response_model=ProjectOut)
def start_pipeline(
    project_id: str,
    force: bool = False,
    force_llm: bool = False,
    user: User = Depends(require_roles(Role.admin)),
    db: Session = Depends(get_db),
):
    """Restart pipeline. force_llm=true runs vision LLM even for text PDFs."""
    project = db.get(Project, _uid(project_id))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    ensure_page_stubs(db, project)
    running = db.scalar(
        select(Job).where(
            Job.project_id == project.id,
            Job.status.in_([JobStatus.queued, JobStatus.running]),
        )
    )
    if running:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Pipeline already running")
    # Confirm whole-book translation (also clears awaiting_confirm for large books).
    project.status = "processing"
    settings = dict(project.settings or {})
    settings["whole_book_confirmed"] = True
    project.settings = settings
    db.commit()
    enqueue_project_pipeline(db, project.id, force=force, force_llm=force_llm)
    return _project_out(db, project)


@router.patch("/{project_id}/settings", response_model=ProjectOut)
def update_settings(
    project_id: str,
    body: ProjectSettingsIn,
    user: User = Depends(require_roles(Role.admin)),
    db: Session = Depends(get_db),
):
    project = db.get(Project, _uid(project_id))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    project.settings = body.settings
    db.commit()
    db.refresh(project)
    return _project_out(db, project)


@router.get("/{project_id}/export.pdf")
def export_pdf(
    project_id: str,
    mode: str = "text",
    rebuild: bool = False,
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    """Build or download PDF. mode=text | interleave.

    By default serves the last built file (fast). Pass rebuild=1 to regenerate
    (can take minutes on a full book — avoid browser timeouts).
    """
    from pathlib import Path

    from app.services.storage import ensure_dirs

    project = db.get(Project, _uid(project_id))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    mode_n = (mode or "text").strip().lower()
    if mode_n not in ("text", "interleave"):
        mode_n = "text"
    suffix = "-interleave" if mode_n == "interleave" else ""
    filename = f"{project.slug}{suffix}.pdf"
    path = ensure_dirs() / "exports" / str(project.id) / filename

    if rebuild or not path.is_file() or path.stat().st_size < 1024:
        pages = list(
            db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
        )
        payload = [
            (p.page_no, p.current_html or "", p.scan_path)
            for p in pages
            if (p.current_html and p.current_html.strip()) or (mode_n == "interleave" and p.scan_path)
        ]
        if not payload:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No page text to export yet")
        try:
            path = build_project_pdf(
                project.id,
                project.slug,
                project.title,
                payload,
                title_sa=project.title_sa,
                mode=mode_n,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"PDF failed: {exc}"
            ) from exc

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
    )


@router.get("/{project_id}/export.docx")
def export_docx(
    project_id: str,
    mode: str = "text",
    rebuild: bool = False,
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    """Build or download DOCX. mode=text | interleave (same as PDF export)."""
    from pathlib import Path

    from app.services.storage import ensure_dirs

    project = db.get(Project, _uid(project_id))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    mode_n = (mode or "text").strip().lower()
    if mode_n not in ("text", "interleave"):
        mode_n = "text"
    suffix = "-interleave" if mode_n == "interleave" else ""
    filename = f"{project.slug}{suffix}.docx"
    path = ensure_dirs() / "exports" / str(project.id) / filename

    if rebuild or not path.is_file() or path.stat().st_size < 512:
        pages = list(
            db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
        )
        payload = [
            (p.page_no, p.current_html or "", p.scan_path)
            for p in pages
            if (p.current_html and p.current_html.strip()) or (mode_n == "interleave" and p.scan_path)
        ]
        if not payload:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No page text to export yet")
        try:
            path = build_project_docx(
                project.id,
                project.slug,
                project.title,
                payload,
                title_sa=project.title_sa,
                mode=mode_n,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"DOCX failed: {exc}"
            ) from exc

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )


@router.post("/{project_id}/extract", response_model=ProjectOut)
def extract_more(
    project_id: str,
    body: ExtractIn,
    user: User = Depends(require_roles(Role.admin, Role.expert)),
    db: Session = Depends(get_db),
):
    """Manual extract range (optional). Prefer auto pipeline after upload."""
    project = db.get(Project, _uid(project_id))
    if project is None or not project.source_pdf_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project/PDF not found")
    pdf_path = Path(project.source_pdf_path)
    extract_to = body.extract_to or body.extract_from
    written = extract_pages(pdf_path, project.id, body.extract_from, extract_to)
    for page_no in written:
        page = db.scalar(select(Page).where(Page.project_id == project.id, Page.page_no == page_no))
        if page is None:
            page = Page(project_id=project.id, page_no=page_no, status=PageStatus.pending)
            db.add(page)
        page.scan_path = str(storage.page_png_path(project.id, page_no))
    db.commit()
    return _project_out(db, project)
