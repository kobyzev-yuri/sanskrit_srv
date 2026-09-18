"""Auto extract + LLM draft pipeline. Consecutive pages may share one LLM call."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import Job, JobStatus, Page, PageStatus, PageVersion, Project, User, VersionSource, utcnow
from app.services import storage
from app.services.layout_assets import extract_embedded_figures, finalize_page_html, preserve_figure_srcs
from app.services.llm_draft import (
    consecutive_page_runs,
    digitize_batch_size_for_plan,
    looks_like_page_html,
    revise_from_scan,
    revise_from_scans,
)
from app.services.llm_proofread import (
    apply_proofread_suggestions,
    gross_draft_items,
    neighbor_html,
    proofread_translation,
    remaining_after_apply,
    save_page_proofread,
)
from app.services.llm_route import llm_user_context
from app.services.llm_status import LlmQuotaError, LlmRateLimitError, set_quota_alert
from app.services.llm_translate import (
    BLANK_IAST_ARTICLE,
    BLANK_RU_ARTICLE,
    english_passthrough_html,
    looks_like_translation_html,
    looks_like_transliteration_html,
    pack_translate_runs,
    source_is_latin_page,
    translate_batch_size_for_plan,
    translate_from_source,
    translate_from_sources,
    transliterate_from_source,
    transliterate_from_sources,
    visible_html_text,
)
from app.services.llm_usage import record_usage
from app.services.openrouter_ox import is_missing_gateway_model_error
from app.services.pdf_extract import (
    classify_pdf,
    extract_page_text_html,
    extract_pages,
    pdf_page_count,
)
from app.services.translation_style import (
    ENGLISH_KEEP,
    lock_translation_template,
    lock_transliteration_template,
    project_task,
    transliteration_agreed,
    transliteration_cfg,
    translation_agreed,
    translation_cfg,
)

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


def resolve_pipeline_user(db: Session, job_id: uuid.UUID | None) -> User | None:
    """Expert who queued the mass job — same keys as a per-page click."""
    if job_id is None:
        return None
    job = db.get(Job, job_id)
    raw = (job.payload or {}).get("user_id") if job is not None else None
    if not raw:
        return None
    try:
        return db.get(User, uuid.UUID(str(raw)))
    except (ValueError, TypeError):
        return None


def _llm_for_job(db: Session, job_id: uuid.UUID | None):
    user = resolve_pipeline_user(db, job_id)
    return llm_user_context(user) if user is not None else nullcontext()


def page_is_agreed(page: Page) -> bool:
    return page.status in AGREED_STATUSES


def translate_accept_status(*, auto_agree: bool) -> PageStatus:
    """open_only («несогласованные»): удачный черновик сразу expert_done, чтобы не гонять его снова."""
    return PageStatus.expert_done if auto_agree else PageStatus.expert_review


def agree_nonempty_translations(db: Session, project: Project) -> list[int]:
    """Согласовать страницы, где уже лежит настоящий русский перевод (не CoT и не blank)."""
    marked: list[int] = []
    pages = list(
        db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
    )
    for page in pages:
        if page_is_agreed(page):
            continue
        html = page.current_html or ""
        cyr = sum(1 for c in html if "\u0400" <= c <= "\u04ff")
        if cyr >= 8 and looks_like_translation_html(html, page.source_html):
            page.status = PageStatus.expert_done
            marked.append(page.page_no)
    if marked:
        db.commit()
    return marked


def agree_nonempty_transliterations(db: Session, project: Project) -> list[int]:
    """Согласовать страницы, где уже лежит настоящая транслитерация IAST."""
    marked: list[int] = []
    cfg = transliteration_cfg(project)
    style = str(cfg.get("style") or "iast_block")
    pages = list(
        db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
    )
    for page in pages:
        if page_is_agreed(page):
            continue
        html = page.current_html or ""
        src = page.source_html or ""
        if looks_like_transliteration_html(html, src, style=style):
            page.status = PageStatus.expert_done
            marked.append(page.page_no)
            continue
        if str(cfg.get("english_comments") or "") == ENGLISH_KEEP and source_is_latin_page(src):
            if not visible_html_text(html):
                page.current_html = english_passthrough_html(src)
            page.status = PageStatus.expert_done
            marked.append(page.page_no)
    if marked:
        db.commit()
    return marked


def agree_nonempty_digitize_drafts(db: Session, project: Project) -> list[int]:
    """Согласовать страницы оцифровки, где уже лежит настоящий HTML (не CoT)."""
    marked: list[int] = []
    pages = list(
        db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
    )
    for page in pages:
        if page_is_agreed(page):
            continue
        html = page.current_html or ""
        if not looks_like_page_html(html):
            continue
        dev = sum(1 for c in html if "\u0900" <= c <= "\u097f")
        if dev >= 8 or (html.lower().count("<p") >= 1 and len(html.strip()) > 80):
            page.status = PageStatus.expert_done
            marked.append(page.page_no)
    if marked:
        db.commit()
    return marked


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


def _translate_wait_heartbeat(job_id: uuid.UUID, page_no: int, stop: threading.Event) -> None:
    """Tick job.progress.wait_s so the UI is not stuck on a silent LLM call."""
    from app.db import get_session_factory

    SessionLocal = get_session_factory()
    started = time.monotonic()
    while True:
        elapsed = int(time.monotonic() - started)
        try:
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                if job is None or job.status != JobStatus.running:
                    return
                set_job_progress(db, job, current_page=page_no, wait_s=elapsed)
        except Exception:  # noqa: BLE001
            log.debug("translate heartbeat failed", exc_info=True)
        if stop.wait(10):
            return


def _looks_like_derived_html(html: str, source_html: str | None, project: Project | None) -> bool:
    task = project_task(project)
    if task == "transliterate":
        cfg = transliteration_cfg(project)
        if looks_like_transliteration_html(
            html, source_html, style=str(cfg.get("style") or "iast_block")
        ):
            return True
        if str(cfg.get("english_comments") or "") == ENGLISH_KEEP and source_is_latin_page(
            source_html or ""
        ):
            return bool(visible_html_text(html))
        return False
    return looks_like_translation_html(html, source_html)


def process_one_translate_page(
    db: Session,
    page: Page,
    *,
    job_id: uuid.UUID | None = None,
    auto_agree: bool = False,
) -> str:
    """LLM Russian translation or IAST transliteration from verified Sanskrit source_html."""
    project = db.get(Project, page.project_id)
    task = project_task(project)
    if project is None or task not in ("translate", "transliterate"):
        raise RuntimeError("not a translate/transliterate project")
    is_iast = task == "transliterate"
    if is_iast:
        if not transliteration_agreed(project):
            lock_transliteration_template(project)
            flag_modified(project, "settings")
            db.commit()
    elif not translation_agreed(project):
        lock_translation_template(project)
        flag_modified(project, "settings")
        db.commit()
    source_html = (page.source_html or "").strip()
    if not source_html:
        return "skip_no_source"
    cfg = transliteration_cfg(project) if is_iast else translation_cfg(project)
    if not visible_html_text(source_html):
        _save_version(
            db,
            page,
            BLANK_IAST_ARTICLE if is_iast else BLANK_RU_ARTICLE,
            VersionSource.llm,
            "empty source page",
            status=translate_accept_status(auto_agree=auto_agree),
        )
        return ("transliterate" if is_iast else "translate") + ":empty_source"
    if is_iast and source_is_latin_page(source_html):
        keep = str(cfg.get("english_comments") or "") == ENGLISH_KEEP
        html = english_passthrough_html(source_html) if keep else BLANK_IAST_ARTICLE
        _save_version(
            db,
            page,
            html,
            VersionSource.llm,
            "english kept from source" if keep else "english dropped (latin-only page)",
            status=translate_accept_status(auto_agree=auto_agree),
        )
        return "transliterate:english_keep" if keep else "transliterate:english_drop"

    if _already_translated_this_job(db, page, job_id):
        return "skip_already_this_job"

    page.status = PageStatus.llm_draft
    db.commit()
    op = "transliterate" if is_iast else "translate"
    recorded: list[int] = []
    stop_wait = threading.Event()
    if job_id is not None:
        threading.Thread(
            target=_translate_wait_heartbeat,
            args=(job_id, page.page_no, stop_wait),
            daemon=True,
        ).start()

    def on_chunk(index: int, total_chunks: int, model: str, usage: dict) -> None:
        record_usage(
            db,
            project_id=project.id,
            page_id=page.id,
            job_id=job_id,
            network=str(usage.get("network") or "openrouter"),
            model=str(usage.get("model") or model.split(":", 1)[-1]),
            usage=usage,
            operation=op,
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
            wait_s=0,
        )

    try:
        runner = transliterate_from_source if is_iast else translate_from_source
        with _llm_for_job(db, job_id):
            html, model, usage = runner(
                source_html=source_html,
                cfg=cfg,
                current_html=None,
                directive=None,
                on_chunk=on_chunk,
            )
    finally:
        stop_wait.set()
    if not recorded:
        record_usage(
            db,
            project_id=project.id,
            page_id=page.id,
            job_id=job_id,
            network=str(usage.get("network") or "openrouter"),
            model=str(usage.get("model") or model.split(":", 1)[-1]),
            usage=usage,
            operation=op,
        )
    _save_version(
        db,
        page,
        html,
        VersionSource.llm,
        f"batch {op} {cfg.get('style')} | {model}",
        status=translate_accept_status(auto_agree=auto_agree),
    )
    return f"{op}:{model}"


def _already_translated_this_job(db: Session, page: Page, job_id: uuid.UUID | None) -> bool:
    if job_id is None:
        return False
    job = db.get(Job, job_id)
    if job is None:
        return False
    return bool(
        (page.current_html or "").strip()
        and page.updated_at
        and job.created_at
        and page.updated_at >= job.created_at
        and page.status in (PageStatus.expert_review, PageStatus.expert_done)
        and _looks_like_derived_html(page.current_html or "", page.source_html, db.get(Project, page.project_id))
    )


def _revert_empty_translate_drafts(db: Session, pages: list[Page]) -> None:
    for p in pages:
        page = db.get(Page, p.id)
        if page is None or page.status != PageStatus.llm_draft:
            continue
        if looks_like_translation_html(page.current_html or "", page.source_html):
            continue
        if looks_like_transliteration_html(page.current_html or "", page.source_html):
            continue
        page.status = PageStatus.pending
    db.commit()


def translate_one_by_one(
    db: Session,
    pages: list[Page],
    *,
    job_id: uuid.UUID | None = None,
    skip_nos: set[int] | None = None,
    auto_agree: bool = False,
) -> list[str]:
    """Per-page translate. One failure does not skip the rest of the run."""
    notes: list[str] = []
    skip = skip_nos or set()
    for p in pages:
        if p.page_no in skip:
            continue
        if job_id:
            job = db.get(Job, job_id)
            if job is not None:
                prog = dict(job.progress or {})
                prog["current_page"] = p.page_no
                job.progress = prog
                db.commit()
        try:
            log.info("translate page %s (one-by-one)", p.page_no)
            notes.append(process_one_translate_page(db, p, job_id=job_id, auto_agree=auto_agree))
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            if is_missing_gateway_model_error(str(exc)):
                raise
            log.exception("single-page translate failed page %s", p.page_no)
            notes.append(f"fail:{p.page_no}")
            page = db.get(Page, p.id)
            if page is not None and not (
                looks_like_translation_html(page.current_html or "", page.source_html)
                or looks_like_transliteration_html(page.current_html or "", page.source_html)
            ):
                page.status = PageStatus.pending
                db.commit()
    return notes


def process_translate_run(
    db: Session,
    pages: list[Page],
    *,
    job_id: uuid.UUID | None = None,
    auto_agree: bool = False,
) -> str:
    """Translate a consecutive run. Falls back to one-page calls if the batch is incomplete."""
    if not pages:
        return "empty"
    if len(pages) == 1:
        return process_one_translate_page(db, pages[0], job_id=job_id, auto_agree=auto_agree)

    project = db.get(Project, pages[0].project_id)
    task = project_task(project)
    if project is None or task not in ("translate", "transliterate"):
        raise RuntimeError("not a translate/transliterate project")
    is_iast = task == "transliterate"
    if is_iast:
        if not transliteration_agreed(project):
            lock_transliteration_template(project)
            flag_modified(project, "settings")
            db.commit()
    elif not translation_agreed(project):
        lock_translation_template(project)
        flag_modified(project, "settings")
        db.commit()

    payloads: list[dict] = []
    for page in pages:
        page = db.get(Page, page.id)
        if page is None:
            continue
        if _already_translated_this_job(db, page, job_id):
            continue
        source_html = (page.source_html or "").strip()
        if not source_html:
            continue
        if is_iast and source_is_latin_page(source_html):
            process_one_translate_page(db, page, job_id=job_id, auto_agree=auto_agree)
            continue
        page.status = PageStatus.llm_draft
        db.commit()
        payloads.append({"page": page, "page_no": page.page_no, "source_html": source_html})
    if len(payloads) <= 1:
        return ",".join(translate_one_by_one(db, pages, job_id=job_id, auto_agree=auto_agree))

    cfg = transliteration_cfg(project) if is_iast else translation_cfg(project)
    batch_runner = transliterate_from_sources if is_iast else translate_from_sources
    op = "transliterate" if is_iast else "translate"
    try:
        with _llm_for_job(db, job_id):
            html_by_no, model, usage = batch_runner(
                [{k: v for k, v in row.items() if k != "page"} for row in payloads],
                cfg=cfg,
            )
    except (LlmQuotaError, LlmRateLimitError):
        _revert_empty_translate_drafts(db, pages)
        raise
    except Exception as exc:  # noqa: BLE001
        if is_missing_gateway_model_error(str(exc)):
            _revert_empty_translate_drafts(db, pages)
            raise
        log.exception(
            "batch translate %s–%s failed; falling back per page",
            pages[0].page_no,
            pages[-1].page_no,
        )
        notes = translate_one_by_one(db, pages, job_id=job_id, auto_agree=auto_agree)
        return "batch-fail; fallback:" + ",".join(notes)

    n_ok = max(1, len(html_by_no))
    piece = _split_usage(usage, n_ok)
    done_nos: set[int] = set()
    for row in payloads:
        page = db.get(Page, row["page"].id)
        if page is None:
            continue
        html = html_by_no.get(page.page_no)
        if not html:
            continue
        record_usage(
            db,
            project_id=project.id,
            page_id=page.id,
            job_id=job_id,
            network=str(usage.get("network") or "openrouter"),
            model=str(usage.get("model") or model.split(":", 1)[-1]),
            usage=piece,
            operation=f"{op}_batch",
        )
        _save_version(
            db,
            page,
            html,
            VersionSource.llm,
            f"batch {op} {cfg.get('style')} {pages[0].page_no}–{pages[-1].page_no} | {model}",
            status=translate_accept_status(auto_agree=auto_agree),
        )
        done_nos.add(page.page_no)

    fallback = translate_one_by_one(
        db, pages, job_id=job_id, skip_nos=done_nos, auto_agree=auto_agree
    )
    note = f"batch:{model}:{pages[0].page_no}-{pages[-1].page_no}:{len(done_nos)}/{len(pages)}"
    if fallback:
        note += "; fallback:" + ",".join(fallback)
    return note


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
    with _llm_for_job(db, job_id):
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
    html, model, usage = None, "", {}
    with _llm_for_job(db, job_id):
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


def _split_usage(usage: dict, n: int) -> dict:
    if n <= 1:
        return dict(usage)
    out = dict(usage)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = out.get(key)
        if val is None:
            continue
        try:
            out[key] = int(val) // n
        except (TypeError, ValueError):
            pass
    return out


def digitize_one_by_one(
    db: Session,
    pages: list[Page],
    *,
    force: bool = False,
    force_llm: bool = False,
    job_id: uuid.UUID | None = None,
    skip_nos: set[int] | None = None,
) -> list[str]:
    """Per-page digitize. One failure does not skip the rest of the run."""
    notes: list[str] = []
    skip = skip_nos or set()
    for p in pages:
        if p.page_no in skip:
            continue
        if job_id:
            job = db.get(Job, job_id)
            if job is not None:
                prog = dict(job.progress or {})
                prog["current_page"] = p.page_no
                job.progress = prog
                db.commit()
        try:
            log.info("digitize page %s (one-by-one)", p.page_no)
            notes.append(
                process_one_page(db, p, force=force, force_llm=force_llm, job_id=job_id)
            )
        except (LlmQuotaError, LlmRateLimitError):
            raise
        except Exception as exc:  # noqa: BLE001
            if is_missing_gateway_model_error(str(exc)):
                raise
            log.exception("single-page digitize failed page %s", p.page_no)
            notes.append(f"fail:{p.page_no}")
            page = db.get(Page, p.id)
            if page is not None and not (page.current_html or "").strip():
                page.status = PageStatus.pending
                db.commit()
    return notes


def process_digitize_run(
    db: Session,
    pages: list[Page],
    *,
    force: bool = False,
    force_llm: bool = False,
    job_id: uuid.UUID | None = None,
) -> str:
    """Digitize a consecutive run. Falls back to one-page calls if the batch is incomplete."""
    if not pages:
        return "empty"
    if len(pages) == 1:
        return process_one_page(
            db, pages[0], force=force, force_llm=force_llm, job_id=job_id
        )
    project = db.get(Project, pages[0].project_id)
    if project is None:
        raise RuntimeError("project missing")
    kind = project_source_kind(project)
    if kind == "text" and not force_llm:
        return ",".join(
            digitize_one_by_one(db, pages, force=force, force_llm=False, job_id=job_id)
        )

    payloads: list[dict] = []
    for page in pages:
        page = db.get(Page, page.id)
        if page is None:
            continue
        ensure_page_scan(db, page)
        page = db.get(Page, page.id)
        if page is None or not page.scan_path or not Path(page.scan_path).exists():
            continue
        figs: list[dict] = []
        try:
            figs = extract_embedded_figures(Path(project.source_pdf_path), project.id, page.page_no)
        except Exception:  # noqa: BLE001
            log.exception("figure extract failed page %s", page.page_no)
        page.status = PageStatus.llm_draft
        db.commit()
        payloads.append(
            {
                "page": page,
                "scan_path": Path(page.scan_path),
                "page_no": page.page_no,
                "current_html": page.current_html,
                "available_figures": figs or None,
                "directive": "Сделай полный HTML-черновик всей страницы по скану, сохранив стиль и компоновку книги.",
            }
        )
    if len(payloads) <= 1:
        return ",".join(
            digitize_one_by_one(db, pages, force=force, force_llm=force_llm, job_id=job_id)
        )

    try:
        with _llm_for_job(db, job_id):
            html_by_no, model, usage = revise_from_scans(
                [{k: v for k, v in row.items() if k != "page"} for row in payloads]
            )
    except (LlmQuotaError, LlmRateLimitError):
        raise
    except Exception as exc:  # noqa: BLE001
        if is_missing_gateway_model_error(str(exc)):
            raise
        log.exception("batch digitize %s–%s failed; falling back per page", pages[0].page_no, pages[-1].page_no)
        notes = digitize_one_by_one(
            db, pages, force=force, force_llm=force_llm, job_id=job_id
        )
        return "batch-fail; fallback:" + ",".join(notes)

    n_ok = max(1, len(html_by_no))
    piece = _split_usage(usage, n_ok)
    done_nos: set[int] = set()
    for row in payloads:
        page = db.get(Page, row["page"].id)
        if page is None:
            continue
        html = html_by_no.get(page.page_no)
        if not html:
            continue
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
            usage=piece,
            operation="auto_draft_batch",
        )
        _save_version(
            db,
            page,
            html,
            VersionSource.llm,
            f"auto {model} batch {pages[0].page_no}–{pages[-1].page_no} (accepted by default)",
        )
        done_nos.add(page.page_no)

    fallback = digitize_one_by_one(
        db,
        pages,
        force=force,
        force_llm=force_llm,
        job_id=job_id,
        skip_nos=done_nos,
    )
    note = f"batch:{model}:{pages[0].page_no}-{pages[-1].page_no}:{len(done_nos)}/{len(pages)}"
    if fallback:
        note += "; fallback:" + ",".join(fallback)
    return note


def run_pipeline_job(db: Session, job: Job) -> None:
    from app.services.llm_route import bind_llm_user, reset_llm_user

    user = resolve_pipeline_user(db, job.id)
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
    translate = (
        bool(payload.get("translate")) or project_task(project) in ("translate", "transliterate")
    ) and not proofread

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
            agreed_now = agree_nonempty_digitize_drafts(db, project)
            if agreed_now:
                log.info(
                    "open_only: auto-agreed %s existing digitize draft(s): %s",
                    len(agreed_now),
                    agreed_now[:40],
                )
            pages = list(
                db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
            )
            pages = [p for p in pages if not page_is_agreed(p)]
        # Remaining unagreed pages (empty / CoT): re-draft even if junk HTML exists.
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
        batch_n = digitize_batch_size_for_plan()
        by_no = {p.page_no: p for p in pages}
        runs = [
            [by_no[n] for n in run]
            for run in consecutive_page_runs([p.page_no for p in pages], max_n=batch_n)
        ]
        job.progress = {
            **(job.progress or {}),
            "batch_pages": batch_n,
        }
        db.commit()
        for run in runs:
            if not run:
                continue
            if job_is_cancelled(db, job):
                log.info("digitize pipeline cancelled at page %s", run[0].page_no)
                project.status = "in_progress"
                db.commit()
                return
            label = (
                str(run[0].page_no)
                if len(run) == 1
                else f"{run[0].page_no}–{run[-1].page_no}"
            )
            job.progress = {
                "done": done,
                "total": total,
                "current_page": label,
                "source_kind": project_source_kind(project),
                "open_only": open_only,
                "scope": "whole_book",
                "batch_pages": batch_n,
            }
            db.commit()
            try:
                note = process_digitize_run(
                    db, run, force=page_force, force_llm=force_llm, job_id=job.id
                )
                log.info("pages %s (%s/%s): %s", label, done + len(run), total, note)
            except LlmQuotaError as exc:
                msg = str(exc)
                set_quota_alert(msg)
                log.error("quota exhausted at page %s: %s", label, msg)
                job.status = JobStatus.failed
                job.error = f"llm_quota at page {label}: {msg}"
                job.progress = {
                    "done": done,
                    "total": total,
                    "current_page": label,
                    "source_kind": project_source_kind(project),
                    "last_error": job.error,
                    "open_only": open_only,
                    "scope": "whole_book",
                    "batch_pages": batch_n,
                }
                project.status = "in_progress"
                db.commit()
                return
            except Exception as exc:  # noqa: BLE001
                if is_missing_gateway_model_error(str(exc)):
                    job.status = JobStatus.failed
                    job.error = str(exc)[:2000]
                    job.progress = {
                        "done": done,
                        "total": total,
                        "current_page": label,
                        "source_kind": project_source_kind(project),
                        "last_error": job.error,
                        "open_only": open_only,
                        "scope": "whole_book",
                        "batch_pages": batch_n,
                    }
                    project.status = "in_progress"
                    db.commit()
                    return
                log.exception("pages %s failed", label)
                for page in run:
                    page = db.get(Page, page.id)
                    if page is None:
                        continue
                    if not (page.current_html or "").strip():
                        page.status = PageStatus.pending
                    db.commit()
                job.progress = {
                    "done": done,
                    "total": total,
                    "current_page": label,
                    "source_kind": project_source_kind(project),
                    "last_error": str(exc)[:500],
                    "open_only": open_only,
                    "scope": "whole_book",
                    "batch_pages": batch_n,
                }
                db.commit()
                continue
            done += len(run)
            job.progress = {
                "done": done,
                "total": total,
                "current_page": None,
                "source_kind": project_source_kind(project),
                "open_only": open_only,
                "scope": "whole_book",
                "batch_pages": batch_n,
            }
            db.commit()

        project.status = "in_progress"
        last_error = str((job.progress or {}).get("last_error") or "").strip()
        if total and done == 0:
            job.status = JobStatus.failed
            job.error = last_error or "оцифровка: ни одна страница не обработана"
        else:
            job.status = JobStatus.done
        job.progress = {
            "done": done,
            "total": total,
            "current_page": None,
            "source_kind": project_source_kind(project),
            "open_only": open_only,
            "scope": "whole_book",
            "last_error": last_error or None,
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
    task = project_task(project)
    if task == "transliterate":
        if not transliteration_agreed(project):
            lock_transliteration_template(project, resolve_pipeline_user(db, job.id))
            flag_modified(project, "settings")
            db.commit()
    elif not translation_agreed(project):
        lock_translation_template(project, resolve_pipeline_user(db, job.id))
        flag_modified(project, "settings")
        db.commit()

    pages = list(
        db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
    )
    if open_only:
        if task == "transliterate":
            agreed_now = agree_nonempty_transliterations(db, project)
            kind = "transliteration"
        else:
            agreed_now = agree_nonempty_translations(db, project)
            kind = "translation"
        if agreed_now:
            log.info(
                "open_only: auto-agreed %s existing %s(s): %s",
                len(agreed_now),
                kind,
                agreed_now[:40],
            )
        pages = list(
            db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.page_no)).all()
        )
        pages = [p for p in pages if not page_is_agreed(p)]
    # Skip pages without Sanskrit source (nothing to translate).
    pages = [p for p in pages if (p.source_html or "").strip()]
    total = len(pages)
    batch_n = translate_batch_size_for_plan()
    work: list[Page] = []
    done = 0
    skipped = 0
    for page in pages:
        fresh = db.get(Page, page.id)
        if fresh is None:
            skipped += 1
            continue
        if _already_translated_this_job(db, fresh, job.id):
            done += 1
            continue
        work.append(fresh)
    runs = pack_translate_runs(work, max_n=batch_n)
    job.progress = {
        "done": done,
        "skipped": skipped,
        "total": total,
        "current_page": None,
        "open_only": open_only,
        "scope": "translate_all",
        "batch_pages": batch_n,
    }
    db.commit()

    idx = 0
    rate_tries = 0
    while idx < len(runs):
        run = runs[idx]
        if not run:
            idx += 1
            rate_tries = 0
            continue
        if job_is_cancelled(db, job):
            log.info("translate pipeline cancelled at page %s", run[0].page_no)
            project.status = "in_progress"
            db.commit()
            return
        label = (
            str(run[0].page_no)
            if len(run) == 1
            else f"{run[0].page_no}–{run[-1].page_no}"
        )
        job.progress = {
            "done": done,
            "skipped": skipped,
            "total": total,
            "current_page": label,
            "open_only": open_only,
            "scope": "translate_all",
            "batch_pages": batch_n,
        }
        db.commit()
        try:
            note = process_translate_run(db, run, job_id=job.id, auto_agree=open_only)
            log.info("translate %s (%s/%s): %s", label, done + len(run), total, note)
            n_ok = 0
            n_fail = 0
            for p in run:
                page = db.get(Page, p.id)
                if page is not None and _already_translated_this_job(db, page, job.id):
                    n_ok += 1
                else:
                    n_fail += 1
            done += n_ok
            skipped += n_fail
            idx += 1
            rate_tries = 0
        except LlmQuotaError as exc:
            msg = str(exc)
            set_quota_alert(msg)
            log.error("quota exhausted at translate %s: %s", label, msg)
            _revert_empty_translate_drafts(db, run)
            job.status = JobStatus.failed
            job.error = f"llm_quota at page {label}: {msg}"
            job.progress = {
                "done": done,
                "skipped": skipped,
                "total": total,
                "current_page": label,
                "last_error": job.error,
                "open_only": open_only,
                "scope": "translate_all",
                "batch_pages": batch_n,
            }
            project.status = "in_progress"
            db.commit()
            return
        except LlmRateLimitError as exc:
            rate_tries += 1
            wait = min(30 * (2 ** (rate_tries - 1)), 180)
            log.warning(
                "translate %s rate-limited try %s/%s, sleep %ss: %s",
                label,
                rate_tries,
                4,
                wait,
                exc,
            )
            _revert_empty_translate_drafts(db, run)
            job.progress = {
                "done": done,
                "skipped": skipped,
                "total": total,
                "current_page": label,
                "last_error": str(exc)[:500],
                "rate_limited": True,
                "open_only": open_only,
                "scope": "translate_all",
                "batch_pages": batch_n,
            }
            db.commit()
            if rate_tries >= 4:
                log.error("translate %s still rate-limited, leave pending", label)
                skipped += len(run)
                idx += 1
                rate_tries = 0
            else:
                time.sleep(wait)
            continue
        except Exception as exc:  # noqa: BLE001
            if is_missing_gateway_model_error(str(exc)):
                log.error("translate %s gateway model missing: %s", label, exc)
                _revert_empty_translate_drafts(db, run)
                job.status = JobStatus.failed
                job.error = str(exc)[:2000]
                job.progress = {
                    "done": done,
                    "skipped": skipped,
                    "total": total,
                    "current_page": label,
                    "last_error": job.error,
                    "open_only": open_only,
                    "scope": "translate_all",
                    "batch_pages": batch_n,
                }
                project.status = "in_progress"
                db.commit()
                return
            log.exception("translate %s failed", label)
            _revert_empty_translate_drafts(db, run)
            job.progress = {
                "done": done,
                "skipped": skipped,
                "total": total,
                "current_page": label,
                "last_error": str(exc)[:500],
                "open_only": open_only,
                "scope": "translate_all",
                "batch_pages": batch_n,
            }
            db.commit()
            skipped += len(run)
            idx += 1
            rate_tries = 0
        job.progress = {
            "done": done,
            "skipped": skipped,
            "total": total,
            "current_page": None,
            "open_only": open_only,
            "scope": "translate_all",
            "batch_pages": batch_n,
        }
        db.commit()

    project.status = "in_progress"
    last_error = str((job.progress or {}).get("last_error") or "").strip()
    if total and done == 0:
        job.status = JobStatus.failed
        job.error = last_error or "конвейер: ни одна страница не обработана"
    else:
        job.status = JobStatus.done
    job.progress = {
        "done": done,
        "skipped": skipped,
        "total": total,
        "current_page": None,
        "open_only": open_only,
        "scope": "translate_all",
        "batch_pages": batch_n,
        "last_error": last_error or None,
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
            if is_missing_gateway_model_error(str(exc)):
                job.status = JobStatus.failed
                job.error = str(exc)[:2000]
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
