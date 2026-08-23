from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user, require_roles
from app.models import Job, JobStatus, Page, PageStatus, Project, Role, User
from app.schemas import (
    ExtractIn,
    JobOut,
    ProjectOut,
    ProjectSettingsIn,
    ProjectUsageOut,
    SpawnTranslationIn,
    TranslationStyleIn,
)
from app.services import storage
from app.services.export_docx import build_project_docx
from app.services.export_pdf import build_project_pdf
from app.services.export_xlsx import build_project_xlsx
from app.services.html_pages import split_html_pages
from app.services.llm_usage import project_usage_summary
from app.services.pdf_extract import classify_pdf, extract_page_text_html, extract_pages, pdf_page_count
from app.services.pipeline import enqueue_project_pipeline, ensure_page_stubs
from app.services.translation_style import (
    ENGLISH_POLICIES,
    STYLES,
    default_translation_settings,
    project_task,
    translation_cfg,
)

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
    pages_n = pdf_pages or count
    threshold = get_settings().large_book_pages
    manual = bool(settings.get("manual_pages")) or (
        bool(pages_n) and pages_n > threshold and job is None and project_task(project) == "digitize"
    )
    trans = settings.get("translation") if isinstance(settings.get("translation"), dict) else None
    src_pid = settings.get("source_project_id")
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
        task=project_task(project),
        manual_pages=manual,
        translation=trans,
        source_project_id=str(src_pid) if src_pid else None,
        confirm_required=False,
        pipeline=JobOut.model_validate(job) if job else None,
        created_at=project.created_at,
    )


def _unique_slug(db: Session, slug: str) -> None:
    if db.scalar(select(Project).where(Project.slug == slug)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Slug already exists")


def _fill_source_html_from_pdf(db: Session, project: Project) -> int:
    pdf_path = Path(project.source_pdf_path)
    pages = list(db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all())
    n = 0
    for page in pages:
        html = extract_page_text_html(pdf_path, page.page_no)
        page.source_html = html
        n += 1
    db.commit()
    return n


def _copy_pages_as_translation_source(db: Session, src: Project, dest: Project) -> int:
    rows = list(db.scalars(select(Page).where(Page.project_id == src.id).order_by(Page.page_no)).all())
    if not rows:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="У исходного проекта нет страниц")
    for p in rows:
        db.add(
            Page(
                project_id=dest.id,
                page_no=p.page_no,
                status=PageStatus.pending,
                source_html=p.current_html,
                current_html=None,
                scan_path=None,
            )
        )
    db.commit()
    return len(rows)


@router.get("", response_model=list[ProjectOut])
def list_projects(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    projects = list(db.scalars(select(Project).order_by(Project.created_at.desc())).all())
    return [_project_out(db, p) for p in projects]


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    slug: str = Form(...),
    title: str = Form(...),
    title_sa: str | None = Form(None),
    file: UploadFile | None = File(None),
    task: str = Form("digitize"),
    source_project_id: str | None = Form(None),
    translation_style: str = Form("interlinear"),
    english_comments: str = Form("replace"),
    translation_notes: str = Form(""),
    user: User = Depends(require_roles(Role.admin)),
    db: Session = Depends(get_db),
):
    """Upload a scan PDF (digitize) or start a Russian translation project.

    Digitize: auto LLM only if page count ≤ LARGE_BOOK_PAGES (default 10).
    Larger books get page stubs only — digitize one page at a time.
    Translate: never auto-runs; expert agrees the template, then translates per page.
    """
    slug = slug.strip().lower()
    _unique_slug(db, slug)
    task_n = "translate" if (task or "").strip().lower() == "translate" else "digitize"
    fname = (file.filename or "").lower() if file is not None else ""

    settings = _default_settings()
    settings["task"] = task_n
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

    if task_n == "translate":
        settings["translation"] = default_translation_settings(
            style=translation_style,
            english_comments=english_comments,
            notes=translation_notes,
        )
        src_id = (source_project_id or "").strip()
        if src_id:
            src = db.get(Project, _uid(src_id))
            if src is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source project not found")
            settings["source_project_id"] = str(src.id)
            settings["source_kind"] = "html"
            project.settings = settings
            db.commit()
            _copy_pages_as_translation_source(db, src, project)
            project.status = "in_progress"
            db.commit()
            db.refresh(project)
            return _project_out(db, project)

        if not file or not fname:
            db.rollback()
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Для перевода нужен PDF/HTML санскрита или исходный проект",
            )
        if fname.endswith((".html", ".htm")):
            raw = (await file.read()).decode("utf-8", errors="replace")
            articles = split_html_pages(raw)
            if not articles:
                db.rollback()
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="HTML пуст")
            settings["source_kind"] = "html"
            project.settings = settings
            for i, html in enumerate(articles, start=1):
                db.add(
                    Page(
                        project_id=project.id,
                        page_no=i,
                        status=PageStatus.pending,
                        source_html=html,
                    )
                )
            project.status = "in_progress"
            db.commit()
            db.refresh(project)
            return _project_out(db, project)

        if not fname.endswith(".pdf"):
            db.rollback()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Нужен PDF или HTML")
        data = await file.read()
        try:
            pdf_path, info = await asyncio.to_thread(_save_and_classify, project_id, file.filename, data)
        except Exception as exc:
            storage.remove_project_files(project_id)
            db.rollback()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"PDF open failed: {exc}") from exc
        if info.get("kind") == "scan":
            storage.remove_project_files(project_id)
            db.rollback()
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Это скан без текстового слоя. Сначала оцифруйте книгу, затем создайте перевод из проекта.",
            )
        project.source_pdf_path = str(pdf_path)
        settings["source_kind"] = "text"
        settings["source_detect"] = {"avg_chars": info["avg_chars"], "samples": info["samples"][:5]}
        project.settings = settings
        db.commit()
        ensure_page_stubs(db, project)
        _fill_source_html_from_pdf(db, project)
        project.status = "in_progress"
        db.commit()
        db.refresh(project)
        return _project_out(db, project)

    # digitize
    if not file or not fname.endswith(".pdf"):
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="PDF required")
    data = await file.read()
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
        settings["manual_pages"] = True
        settings["auto_pipeline"] = False
        project.settings = settings
        project.status = "in_progress"
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


@router.post("/{project_id}/spawn-translation", response_model=ProjectOut, status_code=201)
def spawn_translation(
    project_id: str,
    body: SpawnTranslationIn,
    user: User = Depends(require_roles(Role.admin, Role.expert)),
    db: Session = Depends(get_db),
):
    """New translate project whose left pane is this book's current Sanskrit HTML."""
    src = db.get(Project, _uid(project_id))
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project_task(src) == "translate":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Это уже проект перевода")
    slug = body.slug.strip().lower()
    _unique_slug(db, slug)
    settings = _default_settings()
    settings["task"] = "translate"
    settings["source_kind"] = "html"
    settings["source_project_id"] = str(src.id)
    settings["translation"] = default_translation_settings(
        style=body.style,
        english_comments=body.english_comments,
        notes=body.notes,
    )
    dest = Project(
        slug=slug,
        title=(body.title or f"{src.title} · перевод").strip(),
        title_sa=src.title_sa,
        status="in_progress",
        settings=settings,
        created_by=user.id,
    )
    db.add(dest)
    db.flush()
    _copy_pages_as_translation_source(db, src, dest)
    db.refresh(dest)
    return _project_out(db, dest)


@router.patch("/{project_id}/translation-style", response_model=ProjectOut)
def update_translation_style(
    project_id: str,
    body: TranslationStyleIn,
    user: User = Depends(require_roles(Role.admin, Role.expert)),
    db: Session = Depends(get_db),
):
    """Set / agree / revoke the translation template. LLM translate requires agreed=true."""
    project = db.get(Project, _uid(project_id))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project_task(project) != "translate":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Не проект перевода")
    settings = dict(project.settings or {})
    cfg = translation_cfg(project)
    if body.style is not None:
        if body.style not in STYLES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unknown translation style")
        cfg["style"] = body.style
    if body.english_comments is not None:
        if body.english_comments not in ENGLISH_POLICIES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unknown english_comments policy")
        cfg["english_comments"] = body.english_comments
    if body.notes is not None:
        cfg["notes"] = body.notes.strip()[:4000]
    if body.agree is True:
        cfg["agreed"] = True
        cfg["agreed_by"] = str(user.id)
        cfg["agreed_at"] = datetime.now(timezone.utc).isoformat()
    elif body.agree is False:
        cfg["agreed"] = False
        cfg["agreed_by"] = None
        cfg["agreed_at"] = None
    settings["translation"] = cfg
    project.settings = settings
    db.commit()
    db.refresh(project)
    return _project_out(db, project)


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


@router.get("/{project_id}/export.xlsx")
def export_xlsx(
    project_id: str,
    rebuild: bool = False,
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    """Spreadsheet of the Russian translation: page / Sanskrit / Russian."""
    from app.services.storage import ensure_dirs

    project = db.get(Project, _uid(project_id))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    filename = f"{project.slug}-translation.xlsx"
    path = ensure_dirs() / "exports" / str(project.id) / filename
    if rebuild or not path.is_file() or path.stat().st_size < 256:
        pages = list(
            db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
        )
        payload = [(p.page_no, p.current_html or "") for p in pages if (p.current_html or "").strip()]
        if not payload:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Нет страниц с переводом для выгрузки")
        try:
            path = build_project_xlsx(
                project.id,
                project.slug,
                project.title,
                payload,
                title_sa=project.title_sa,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"XLSX failed: {exc}"
            ) from exc
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
