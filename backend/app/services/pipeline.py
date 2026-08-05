"""Auto extract + LLM draft pipeline (one page at a time for small VPS)."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Job, JobStatus, Page, PageStatus, PageVersion, Project, VersionSource
from app.services import storage
from app.services.layout_assets import extract_embedded_figures, finalize_page_html
from app.services.llm_draft import revise_from_scan
from app.services.llm_status import LlmQuotaError, set_quota_alert
from app.services.llm_usage import record_usage
from app.services.pdf_extract import (
    classify_pdf,
    extract_page_text_html,
    extract_pages,
    pdf_page_count,
    seed_html,
)

log = logging.getLogger("sanskrit.pipeline")

DEFAULT_REVIEW_DIRECTIVE = (
    "Пересмотри страницу полностью по скану. HTML только классами (page-style, narrow, shloka, "
    "indent, centered, running-head, page-num) — без style=, flex и float. Текст построчно как в книге, "
    "обе колонки до конца, без пустых строк-заглушек."
)



def enqueue_project_pipeline(
    db: Session,
    project_id: uuid.UUID,
    *,
    force: bool = False,
    force_llm: bool = False,
) -> Job:
    job = Job(
        kind="pipeline_project",
        project_id=project_id,
        status=JobStatus.queued,
        payload={"force": force, "force_llm": force_llm},
        progress={"done": 0, "total": 0, "current_page": None},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def project_source_kind(project: Project) -> str:
    settings = project.settings or {}
    return settings.get("source_kind") or "scan"


def ensure_page_stubs(db: Session, project: Project) -> int:
    """Create pending Page rows for every PDF page. Returns total pages."""
    if not project.source_pdf_path:
        raise RuntimeError("project has no PDF")
    total = pdf_page_count(Path(project.source_pdf_path))
    existing_nos = set(
        db.scalars(select(Page.page_no).where(Page.project_id == project.id)).all()
    )
    for page_no in range(1, total + 1):
        if page_no in existing_nos:
            continue
        db.add(
            Page(
                project_id=project.id,
                page_no=page_no,
                status=PageStatus.pending,
                current_html=None,
            )
        )
    db.commit()
    return total


def page_needs_llm_draft(db: Session, page: Page, force: bool = False) -> bool:
    if force:
        return True
    if page.status in (PageStatus.expert_done, PageStatus.scholar_review, PageStatus.published):
        return False
    has_llm = db.scalar(
        select(func.count())
        .select_from(PageVersion)
        .where(PageVersion.page_id == page.id, PageVersion.source == VersionSource.llm)
    )
    if has_llm and page.current_html and len(page.current_html.strip()) > 40:
        return False
    return True


def page_needs_text_extract(db: Session, page: Page, force: bool = False) -> bool:
    if force and not page.current_html:
        return True
    if page.current_html and len(page.current_html.strip()) > 40:
        return False
    if page.status in (PageStatus.scholar_review, PageStatus.published):
        return False
    return True


def _save_version(
    db: Session,
    page: Page,
    html: str,
    source: VersionSource,
    note: str,
) -> None:
    page.current_html = html
    page.status = PageStatus.expert_done
    next_ver = (
        db.scalar(select(func.max(PageVersion.version)).where(PageVersion.page_id == page.id)) or 0
    ) + 1
    db.add(
        PageVersion(
            page_id=page.id,
            version=next_ver,
            html=html,
            source=source,
            note=note,
        )
    )
    db.commit()


def process_one_page(
    db: Session,
    page: Page,
    *,
    force: bool = False,
    force_llm: bool = False,
    job_id: uuid.UUID | None = None,
) -> str:
    """Extract preview image; text-PDF → native text; scan → LLM draft."""
    project = db.get(Project, page.project_id)
    if project is None or not project.source_pdf_path:
        raise RuntimeError("project/pdf missing")

    actions: list[str] = []
    pdf_path = Path(project.source_pdf_path)
    kind = project_source_kind(project)
    scan = Path(page.scan_path) if page.scan_path else storage.page_png_path(project.id, page.page_no)
    if not scan.exists():
        page.status = PageStatus.extracting
        db.commit()
        extract_pages(pdf_path, project.id, page.page_no, page.page_no)
        page.scan_path = str(scan)
        actions.append("extracted")
        db.commit()

    # Born-digital / text PDF: never call LLM unless explicitly forced.
    if kind == "text" and not force_llm:
        if not page_needs_text_extract(db, page, force=force):
            return "skip_text:" + ",".join(actions or ["ok"])
        html = extract_page_text_html(pdf_path, page.page_no)
        _save_version(db, page, html, VersionSource.ocr, "native PDF text (no LLM)")
        actions.append("native_text")
        return ",".join(actions)

    if not page_needs_llm_draft(db, page, force=force or force_llm):
        if page.status == PageStatus.pending:
            page.status = PageStatus.expert_done if page.current_html else PageStatus.pending
            db.commit()
        return "skip_draft:" + ",".join(actions or ["ok"])

    page.status = PageStatus.llm_draft
    db.commit()
    figs: list[dict] = []
    try:
        figs = extract_embedded_figures(pdf_path, project.id, page.page_no)
    except Exception:  # noqa: BLE001
        log.exception("figure extract failed page %s", page.page_no)
    html, model, usage = revise_from_scan(
        Path(page.scan_path),
        page_no=page.page_no,
        current_html=page.current_html,
        directive="Сделай полный HTML-черновик всей страницы по скану, сохранив стиль и компоновку книги.",
        available_figures=figs or None,
    )
    html = finalize_page_html(
        html,
        scan_path=Path(page.scan_path),
        project_id=project.id,
        page_no=page.page_no,
        page_id=page.id,
    )
    record_usage(
        db,
        project_id=project.id,
        page_id=page.id,
        job_id=job_id,
        network=str(usage.get("network") or "gemini"),
        model=str(usage.get("model") or model.split(":", 1)[-1]),
        usage=usage,
        operation="auto_draft",
    )
    _save_version(db, page, html, VersionSource.llm, f"auto {model} (accepted by default)")
    actions.append(f"llm:{model}")
    return ",".join(actions)


def run_pipeline_job(db: Session, job: Job) -> None:
    job.status = JobStatus.running
    job.error = None
    db.commit()

    project = db.get(Project, job.project_id)
    if project is None:
        job.status = JobStatus.failed
        job.error = "project not found"
        db.commit()
        return

    force = bool((job.payload or {}).get("force"))
    force_llm = bool((job.payload or {}).get("force_llm"))
    try:
        # Classify once if not set
        settings = dict(project.settings or {})
        if "source_kind" not in settings and project.source_pdf_path:
            info = classify_pdf(Path(project.source_pdf_path))
            settings["source_kind"] = info["kind"]
            settings["source_detect"] = {
                "avg_chars": info["avg_chars"],
                "samples": info["samples"][:5],
            }
            project.settings = settings
            db.commit()
            log.info(
                "project %s classified as %s (avg_chars=%s)",
                project.id,
                info["kind"],
                info["avg_chars"],
            )

        total = ensure_page_stubs(db, project)
        pages = list(
            db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
        )
        job.progress = {
            "done": 0,
            "total": total,
            "current_page": None,
            "source_kind": project_source_kind(project),
        }
        db.commit()

        done = 0
        for page in pages:
            # refresh page from DB each time
            page = db.get(Page, page.id)
            if page is None:
                continue
            job.progress = {
                "done": done,
                "total": total,
                "current_page": page.page_no,
                "source_kind": project_source_kind(project),
            }
            db.commit()
            try:
                note = process_one_page(db, page, force=force, force_llm=force_llm, job_id=job.id)
                log.info("page %s/%s: %s", page.page_no, total, note)
            except LlmQuotaError as exc:
                # Stop whole-book run — no point continuing without funds.
                msg = str(exc)
                set_quota_alert(msg)
                log.error("quota exhausted at page %s: %s", page.page_no, msg)
                job.status = JobStatus.failed
                job.error = f"llm_quota at page {page.page_no}: {msg}"
                job.progress = {
                    "done": done,
                    "total": total,
                    "current_page": page.page_no,
                    "source_kind": project_source_kind(project),
                    "last_error": job.error,
                    "scope": "whole_book",
                }
                project.status = "in_progress"
                db.commit()
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("page %s failed", page.page_no)
                page = db.get(Page, page.id)
                if page is not None:
                    if not page.current_html:
                        page.current_html = seed_html(page.page_no)
                    # leave for expert; continue rest of the book
                    if page.status == PageStatus.pending:
                        page.status = PageStatus.expert_review
                    db.commit()
                job.progress = {
                    "done": done,
                    "total": total,
                    "current_page": page.page_no if page else None,
                    "source_kind": project_source_kind(project),
                    "last_error": str(exc)[:500],
                    "scope": "whole_book",
                }
                db.commit()
            done += 1
            job.progress = {
                "done": done,
                "total": total,
                "current_page": None,
                "source_kind": project_source_kind(project),
                "scope": "whole_book",
            }
            db.commit()

        project.status = "in_progress"
        job.status = JobStatus.done
        job.progress = {
            "done": done,
            "total": total,
            "current_page": None,
            "source_kind": project_source_kind(project),
            "scope": "whole_book",
        }
        db.commit()
    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.failed
        job.error = str(exc)[:2000]
        db.commit()
        raise
