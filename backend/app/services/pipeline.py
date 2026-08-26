"""Auto extract + LLM draft pipeline (one page at a time for small VPS)."""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import Job, JobStatus, Page, PageStatus, PageVersion, Project, User, VersionSource, utcnow
from app.services import storage
from app.services.layout_assets import extract_embedded_figures, finalize_page_html, preserve_figure_srcs
from app.services.llm_draft import revise_from_scan
from app.services.llm_proofread import (
    apply_proofread_suggestions,
    gross_draft_items,
    neighbor_html,
    proofread_translation,
    remaining_after_apply,
    save_page_proofread,
)
from app.services.llm_status import LlmQuotaError, LlmRateLimitError, set_quota_alert
from app.services.llm_translate import looks_like_translation_html, translate_from_source
from app.services.llm_usage import record_usage
from app.services.pdf_extract import (
    classify_pdf,
    extract_page_text_html,
    extract_pages,
    pdf_page_count,
    seed_html,
)
from app.services.translation_style import project_task, translation_agreed, translation_cfg

log = logging.getLogger("sanskrit.pipeline")

DEFAULT_REVIEW_DIRECTIVE = (
    "Пересмотри страницу полностью по скану. HTML только классами (page-style, narrow, shloka, "
    "indent, centered, running-head, page-num, toc) — без style=, flex и float. "
    "Двухколоночное оглавление: одна таблица class=toc на всю страницу, ровно 4 ячейки в ряду "
    "(лево|стр|право|стр), без второй узкой таблицы внизу. Текст построчно, обе колонки до конца."
)

AGREED_STATUSES = (PageStatus.expert_done, PageStatus.scholar_review, PageStatus.published)


def job_progress_dict(job: Job) -> dict:
    raw = job.progress or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    return dict(raw) if isinstance(raw, dict) else {}


def set_job_progress(db: Session, job: Job, **fields) -> None:
    """Assign a fresh progress dict so SQLite JSON actually persists."""
    prog = job_progress_dict(job)
    prog.update(fields)
    job.progress = prog
    job.updated_at = utcnow()
    flag_modified(job, "progress")
    db.commit()


def job_is_cancelled(db: Session, job: Job) -> bool:
    db.refresh(job, attribute_names=["status"])
    return job.status == JobStatus.cancelled


def enqueue_project_pipeline(
    db: Session,
    project_id: uuid.UUID,
    *,
    force: bool = False,
    force_llm: bool = False,
    open_only: bool = False,
    translate: bool = False,
    proofread: bool = False,
    user_id: uuid.UUID | None = None,
) -> Job:
    job = Job(
        kind="pipeline_project",
        project_id=project_id,
        status=JobStatus.queued,
        payload={
            "force": force,
            "force_llm": force_llm,
            "open_only": open_only,
            "translate": bool(translate) and not proofread,
            "proofread": bool(proofread),
            "user_id": str(user_id) if user_id else None,
        },
        progress={"done": 0, "total": 0, "current_page": None},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def page_is_agreed(page: Page) -> bool:
    return page.status in AGREED_STATUSES


def project_source_kind(project: Project) -> str:
    settings = project.settings or {}
    return settings.get("source_kind") or "scan"


def ensure_page_scan(db: Session, page: Page) -> bool:
    """Extract one page PNG from the source PDF if missing. Returns True if a scan file exists."""
    if page.scan_path and Path(page.scan_path).exists():
        return True
    project = db.get(Project, page.project_id)
    if project is None or not project.source_pdf_path:
        return False
    pdf_path = Path(project.source_pdf_path)
    if not pdf_path.exists():
        return False
    extract_pages(pdf_path, project.id, page.page_no, page.page_no)
    scan = storage.page_png_path(project.id, page.page_no)
    if not scan.exists():
        return False
    page.scan_path = str(scan)
    db.commit()
    return True


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
    *,
    status: PageStatus = PageStatus.expert_done,
) -> None:
    page.current_html = html
    page.status = status
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


def process_one_translate_page(
    db: Session,
    page: Page,
    *,
    job_id: uuid.UUID | None = None,
) -> str:
    """LLM Russian translation from verified Sanskrit source_html."""
    project = db.get(Project, page.project_id)
    if project is None or project_task(project) != "translate":
        raise RuntimeError("not a translate project")
    if not translation_agreed(project):
        raise RuntimeError("translation style not agreed")
    source_html = (page.source_html or "").strip()
    if not source_html:
        return "skip_no_source"

    # Resume after worker restart: skip pages already written by this job.
    if job_id is not None:
        job = db.get(Job, job_id)
        if (
            job is not None
            and (page.current_html or "").strip()
            and page.updated_at
            and job.created_at
            and page.updated_at >= job.created_at
            and page.status in (PageStatus.expert_review, PageStatus.expert_done)
            and looks_like_translation_html(page.current_html or "")
        ):
            return "skip_already_this_job"

    page.status = PageStatus.llm_draft
    db.commit()
    cfg = translation_cfg(project)
    recorded: list[int] = []

    def on_chunk(index: int, total_chunks: int, model: str, usage: dict) -> None:
        record_usage(
            db,
            project_id=project.id,
            page_id=page.id,
            job_id=job_id,
            network=str(usage.get("network") or "openrouter"),
            model=str(usage.get("model") or model.split(":", 1)[-1]),
            usage=usage,
            operation="translate",
        )
        recorded.append(index)
        if job_id is None:
            return
        job = db.get(Job, job_id)
        if job is None:
            return
        set_job_progress(
            db,
            job,
            current_page=page.page_no,
            chunk=index,
            chunks=total_chunks,
            model=model,
        )

    html, model, usage = translate_from_source(
        source_html=source_html,
        cfg=cfg,
        current_html=None,
        directive=None,
        on_chunk=on_chunk,
    )
    if not recorded:
        record_usage(
            db,
            project_id=project.id,
            page_id=page.id,
            job_id=job_id,
            network=str(usage.get("network") or "openrouter"),
            model=str(usage.get("model") or model.split(":", 1)[-1]),
            usage=usage,
            operation="translate",
        )
    _save_version(
        db,
        page,
        html,
        VersionSource.llm,
        f"batch translate {cfg.get('style')} | {model}",
        status=PageStatus.expert_review,
    )
    return f"translate:{model}"


def process_one_translate_proofread(
    db: Session,
    page: Page,
    *,
    job_id: uuid.UUID | None = None,
) -> str:
    """Sense-check one translation page; auto-apply high-severity draft holes on open pages."""
    project = db.get(Project, page.project_id)
    if project is None or project_task(project) != "translate":
        raise RuntimeError("not a translate project")
    draft = (page.current_html or "").strip()
    source = (page.source_html or "").strip()
    if not draft:
        return "skip_no_draft"
    if not source:
        return "skip_no_source"

    nb = neighbor_html(db, page)
    cfg = translation_cfg(project)
    suggestions, model, usage = proofread_translation(
        page_no=page.page_no,
        source_html=source,
        current_html=draft,
        prev_draft=nb["prev_draft"],
        prev_source=nb["prev_source"],
        next_draft=nb["next_draft"],
        next_source=nb["next_source"],
        style=str(cfg.get("style") or "interlinear"),
    )
    record_usage(
        db,
        project_id=project.id,
        page_id=page.id,
        job_id=job_id,
        network=str(usage.get("network") or "openrouter"),
        model=str(usage.get("model") or model.split(":", 1)[-1]),
        usage=usage,
        operation="proofread",
    )

    applied: list[dict[str, str]] = []
    html = page.current_html or ""
    auto = gross_draft_items(suggestions)
    if auto and not page_is_agreed(page):
        html, applied = apply_proofread_suggestions(html, auto)
        if applied:
            html = preserve_figure_srcs(page.source_html or "", html)
            bits = "; ".join(f"{a['wrong'][:40]}→…" for a in applied)
            _save_version(
                db,
                page,
                html,
                VersionSource.llm,
                f"proofread auto high | {model} | {bits}"[:500],
                status=PageStatus.expert_review,
            )

    leftover = remaining_after_apply(suggestions, applied)
    note = (
        f"Автоправок грубых: {len(applied)}. Осталось предложений: {len(leftover)}."
        if applied or leftover
        else "Подозрительных мест не найдено."
    )
    save_page_proofread(
        page.project_id,
        page.id,
        suggestions=leftover,
        model=model,
        note=note,
        job_id=str(job_id) if job_id else None,
    )
    return f"proofread:{model}:high={len(applied)}:left={len(leftover)}"


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
    from app.services.llm_route import bind_llm_user, reset_llm_user

    raw = (job.payload or {}).get("user_id")
    user = None
    if raw:
        try:
            user = db.get(User, uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            user = None
    token = bind_llm_user(user)
    try:
        _run_pipeline_job_body(db, job)
    finally:
        reset_llm_user(token)


def _run_pipeline_job_body(db: Session, job: Job) -> None:
    job.status = JobStatus.running
    job.error = None
    db.commit()

    project = db.get(Project, job.project_id)
    if project is None:
        job.status = JobStatus.failed
        job.error = "project not found"
        db.commit()
        return

    payload = job.payload or {}
    force = bool(payload.get("force"))
    force_llm = bool(payload.get("force_llm"))
    open_only = bool(payload.get("open_only"))
    proofread = bool(payload.get("proofread"))
    translate = (bool(payload.get("translate")) or project_task(project) == "translate") and not proofread

    try:
        if proofread:
            _run_translate_proofread(db, job, project, open_only=open_only)
            return
        if translate:
            _run_translate_pipeline(db, job, project, open_only=open_only)
            return

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

        ensure_page_stubs(db, project)
        pages = list(
            db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
        )
        if open_only:
            pages = [p for p in pages if not page_is_agreed(p)]
        # User re-run with filter: re-draft selected pages even if a draft exists.
        page_force = force or open_only
        total = len(pages)
        job.progress = {
            "done": 0,
            "total": total,
            "current_page": None,
            "source_kind": project_source_kind(project),
            "open_only": open_only,
            "scope": "whole_book",
        }
        db.commit()

        done = 0
        for page in pages:
            page = db.get(Page, page.id)
            if page is None:
                continue
            if job_is_cancelled(db, job):
                log.info("digitize pipeline cancelled at page %s", page.page_no)
                project.status = "in_progress"
                db.commit()
                return
            job.progress = {
                "done": done,
                "total": total,
                "current_page": page.page_no,
                "source_kind": project_source_kind(project),
                "open_only": open_only,
                "scope": "whole_book",
            }
            db.commit()
            try:
                note = process_one_page(
                    db, page, force=page_force, force_llm=force_llm, job_id=job.id
                )
                log.info("page %s/%s: %s", page.page_no, total, note)
            except LlmQuotaError as exc:
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
                    "open_only": open_only,
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
                    if page.status == PageStatus.pending:
                        page.status = PageStatus.expert_review
                    db.commit()
                job.progress = {
                    "done": done,
                    "total": total,
                    "current_page": page.page_no if page else None,
                    "source_kind": project_source_kind(project),
                    "last_error": str(exc)[:500],
                    "open_only": open_only,
                    "scope": "whole_book",
                }
                db.commit()
            done += 1
            job.progress = {
                "done": done,
                "total": total,
                "current_page": None,
                "source_kind": project_source_kind(project),
                "open_only": open_only,
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
            "open_only": open_only,
            "scope": "whole_book",
        }
        db.commit()
    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.failed
        job.error = str(exc)[:2000]
        db.commit()
        raise


def _run_translate_pipeline(
    db: Session,
    job: Job,
    project: Project,
    *,
    open_only: bool,
) -> None:
    if not translation_agreed(project):
        job.status = JobStatus.failed
        job.error = "translation style not agreed"
        db.commit()
        return

    pages = list(
        db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
    )
    if open_only:
        pages = [p for p in pages if not page_is_agreed(p)]
    # Skip pages without Sanskrit source (nothing to translate).
    pages = [p for p in pages if (p.source_html or "").strip()]
    total = len(pages)
    job.progress = {
        "done": 0,
        "skipped": 0,
        "total": total,
        "current_page": None,
        "open_only": open_only,
        "scope": "translate_all",
    }
    db.commit()

    done = 0
    skipped = 0
    idx = 0
    rate_tries = 0
    while idx < len(pages):
        page = db.get(Page, pages[idx].id)
        if page is None:
            idx += 1
            rate_tries = 0
            continue
        if job_is_cancelled(db, job):
            log.info("translate pipeline cancelled at page %s", page.page_no)
            project.status = "in_progress"
            db.commit()
            return
        job.progress = {
            "done": done,
            "skipped": skipped,
            "total": total,
            "current_page": page.page_no,
            "open_only": open_only,
            "scope": "translate_all",
        }
        db.commit()
        try:
            note = process_one_translate_page(db, page, job_id=job.id)
            log.info("translate page %s/%s: %s", page.page_no, total, note)
            done += 1
            idx += 1
            rate_tries = 0
        except LlmQuotaError as exc:
            msg = str(exc)
            set_quota_alert(msg)
            log.error("quota exhausted at translate page %s: %s", page.page_no, msg)
            job.status = JobStatus.failed
            job.error = f"llm_quota at page {page.page_no}: {msg}"
            job.progress = {
                "done": done,
                "skipped": skipped,
                "total": total,
                "current_page": page.page_no,
                "last_error": job.error,
                "open_only": open_only,
                "scope": "translate_all",
            }
            project.status = "in_progress"
            db.commit()
            return
        except LlmRateLimitError as exc:
            rate_tries += 1
            wait = min(30 * (2 ** (rate_tries - 1)), 180)
            log.warning(
                "translate page %s rate-limited try %s/%s, sleep %ss: %s",
                page.page_no,
                rate_tries,
                4,
                wait,
                exc,
            )
            page = db.get(Page, page.id)
            if page is not None and page.status == PageStatus.llm_draft:
                page.status = PageStatus.pending
            job.progress = {
                "done": done,
                "skipped": skipped,
                "total": total,
                "current_page": page.page_no if page else None,
                "last_error": str(exc)[:500],
                "rate_limited": True,
                "open_only": open_only,
                "scope": "translate_all",
            }
            db.commit()
            if rate_tries >= 4:
                log.error("translate page %s still rate-limited, leave pending", page.page_no if page else "?")
                skipped += 1
                idx += 1
                rate_tries = 0
            else:
                time.sleep(wait)
            continue
        except Exception as exc:  # noqa: BLE001
            log.exception("translate page %s failed", page.page_no)
            page = db.get(Page, page.id)
            if page is not None and page.status == PageStatus.llm_draft:
                page.status = (
                    PageStatus.expert_review
                    if looks_like_translation_html(page.current_html or "")
                    else PageStatus.pending
                )
                db.commit()
            job.progress = {
                "done": done,
                "skipped": skipped,
                "total": total,
                "current_page": page.page_no if page else None,
                "last_error": str(exc)[:500],
                "open_only": open_only,
                "scope": "translate_all",
            }
            db.commit()
            skipped += 1
            idx += 1
            rate_tries = 0
        job.progress = {
            "done": done,
            "skipped": skipped,
            "total": total,
            "current_page": None,
            "open_only": open_only,
            "scope": "translate_all",
        }
        db.commit()

    project.status = "in_progress"
    job.status = JobStatus.done
    job.progress = {
        "done": done,
        "skipped": skipped,
        "total": total,
        "current_page": None,
        "open_only": open_only,
        "scope": "translate_all",
    }
    db.commit()


def _run_translate_proofread(
    db: Session,
    job: Job,
    project: Project,
    *,
    open_only: bool,
) -> None:
    if project_task(project) != "translate":
        job.status = JobStatus.failed
        job.error = "proofread-all is for translation projects"
        db.commit()
        return

    pages = list(
        db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
    )
    if open_only:
        pages = [p for p in pages if not page_is_agreed(p)]
    pages = [p for p in pages if (p.current_html or "").strip() and (p.source_html or "").strip()]
    total = len(pages)
    set_job_progress(
        db,
        job,
        done=0,
        skipped=0,
        applied_high=0,
        flagged=0,
        total=total,
        current_page=None,
        open_only=open_only,
        scope="translate_proofread",
        checked=[],
    )

    done = 0
    skipped = 0
    applied_high = 0
    flagged = 0
    checked: list[int] = []
    idx = 0
    rate_tries = 0
    while idx < len(pages):
        page = db.get(Page, pages[idx].id)
        if page is None:
            idx += 1
            rate_tries = 0
            continue
        if page.page_no in checked:
            idx += 1
            continue
        if job_is_cancelled(db, job):
            log.info("proofread pipeline cancelled at page %s", page.page_no)
            project.status = "in_progress"
            db.commit()
            return
        set_job_progress(
            db,
            job,
            done=done,
            skipped=skipped,
            applied_high=applied_high,
            flagged=flagged,
            total=total,
            current_page=page.page_no,
            open_only=open_only,
            scope="translate_proofread",
            checked=checked,
        )
        try:
            note = process_one_translate_proofread(db, page, job_id=job.id)
            log.info("proofread translate page %s/%s: %s", page.page_no, total, note)
            if note.startswith("skip_"):
                skipped += 1
            else:
                done += 1
                # Re-read leftover count from note: proofread:...:high=N:left=M
                if ":high=" in note:
                    try:
                        applied_high += int(note.split(":high=")[1].split(":")[0])
                        flagged += int(note.split(":left=")[1])
                    except (IndexError, ValueError):
                        pass
            checked.append(page.page_no)
            idx += 1
            rate_tries = 0
        except LlmQuotaError as exc:
            msg = str(exc)
            set_quota_alert(msg)
            log.error("quota exhausted at proofread page %s: %s", page.page_no, msg)
            job.status = JobStatus.failed
            job.error = f"llm_quota at page {page.page_no}: {msg}"
            set_job_progress(
                db,
                job,
                done=done,
                skipped=skipped,
                applied_high=applied_high,
                flagged=flagged,
                total=total,
                current_page=page.page_no,
                last_error=job.error,
                open_only=open_only,
                scope="translate_proofread",
                checked=checked,
            )
            project.status = "in_progress"
            db.commit()
            return
        except LlmRateLimitError as exc:
            rate_tries += 1
            wait = min(30 * (2 ** (rate_tries - 1)), 180)
            log.warning(
                "proofread page %s rate-limited try %s/%s, sleep %ss: %s",
                page.page_no,
                rate_tries,
                4,
                wait,
                exc,
            )
            set_job_progress(
                db,
                job,
                done=done,
                skipped=skipped,
                applied_high=applied_high,
                flagged=flagged,
                total=total,
                current_page=page.page_no,
                last_error=str(exc)[:500],
                rate_limited=True,
                open_only=open_only,
                scope="translate_proofread",
                checked=checked,
            )
            if rate_tries >= 4:
                skipped += 1
                checked.append(page.page_no)
                idx += 1
                rate_tries = 0
            else:
                time.sleep(wait)
            continue
        except Exception as exc:  # noqa: BLE001
            log.exception("proofread translate page %s failed", page.page_no)
            set_job_progress(
                db,
                job,
                done=done,
                skipped=skipped,
                applied_high=applied_high,
                flagged=flagged,
                total=total,
                current_page=page.page_no,
                last_error=str(exc)[:500],
                open_only=open_only,
                scope="translate_proofread",
                checked=checked,
            )
            skipped += 1
            checked.append(page.page_no)
            idx += 1
            rate_tries = 0

    project.status = "in_progress"
    job.status = JobStatus.done
    set_job_progress(
        db,
        job,
        done=done,
        skipped=skipped,
        applied_high=applied_high,
        flagged=flagged,
        total=total,
        current_page=None,
        open_only=open_only,
        scope="translate_proofread",
        checked=checked,
    )
