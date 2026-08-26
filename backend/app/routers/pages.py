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
    DraftSearchOut,
    PageDetailOut,
    PageHtmlIn,
    PageOut,
    PageReviewAgainIn,
    PageReviseIn,
    PageTranslateIn,
    PageVersionOut,
    ProofreadApplyIn,
    ProofreadOut,
    ProofreadSuggestion,
)
from app.services.directive_fix import apply_directive_replacements
from app.services.draft_search import search_pages
from app.services.layout_assets import (
    extract_embedded_figures,
    finalize_page_html,
    figure_file,
    preserve_figure_srcs,
)
from app.services.llm_draft import revise_from_scan
from app.services.llm_proofread import (
    apply_proofread_suggestions,
    load_page_proofread,
    neighbor_html,
    proofread_counts,
    proofread_from_scan,
    proofread_translation,
    save_page_proofread,
    split_by_target,
)
from app.services.llm_status import LlmQuotaError
from app.services.llm_translate import translate_from_source
from app.services.llm_usage import record_usage
from app.services.pipeline import DEFAULT_REVIEW_DIRECTIVE, ensure_page_scan, process_one_page
from app.services.source_sync import sync_sanskrit_to_digitize
from app.services.translation_style import project_task, translation_agreed, translation_cfg

router = APIRouter(tags=["pages"])


def _uid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found") from exc


def _proofread_out(stored: dict | None) -> ProofreadOut | None:
    if not stored:
        return None
    items = stored.get("suggestions") or []
    suggestions = []
    for s in items:
        if not isinstance(s, dict):
            continue
        try:
            suggestions.append(ProofreadSuggestion(**s))
        except Exception:  # noqa: BLE001
            continue
    if not suggestions:
        return None
    return ProofreadOut(
        suggestions=suggestions,
        model=str(stored.get("model") or ""),
        note=str(stored.get("note") or ""),
    )


def _page_out(page: Page, *, proof_n: int = 0) -> PageOut:
    return PageOut(
        id=page.id,
        project_id=page.project_id,
        page_no=page.page_no,
        status=page.status,
        has_scan=bool(page.scan_path and Path(page.scan_path).exists()),
        has_html=bool(page.current_html),
        has_source_html=bool((page.source_html or "").strip()),
        proof_n=proof_n,
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
    counts = proofread_counts(pid)
    return [_page_out(p, proof_n=counts.get(str(p.id), 0)) for p in db.scalars(q).all()]


@router.get("/projects/{project_id}/search", response_model=DraftSearchOut)
def search_project_draft(
    project_id: str,
    q: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Substring search in page drafts (and Sanskrit source on translate projects)."""
    pid = _uid(project_id)
    project = db.get(Project, pid)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    query = (q or "").strip()
    if not query:
        return DraftSearchOut(query="")
    pages = list(
        db.scalars(select(Page).where(Page.project_id == pid).order_by(Page.page_no)).all()
    )
    return DraftSearchOut.model_validate(
        search_pages(pages, query, include_source=project_task(project) == "translate")
    )


@router.get("/pages/{page_id}", response_model=PageDetailOut)
def get_page(page_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    project = db.get(Project, page.project_id)
    if project is not None and project_task(project) != "translate":
        ensure_page_scan(db, page)
        db.refresh(page)
    # Translate drafts: LLM often corrupts figure UUIDs — restore from source_html.
    if (
        project is not None
        and project_task(project) == "translate"
        and (page.source_html or "").strip()
        and (page.current_html or "").strip()
    ):
        fixed = preserve_figure_srcs(page.source_html or "", page.current_html or "")
        if fixed != (page.current_html or ""):
            page.current_html = fixed
            db.commit()
            db.refresh(page)
    scan_url = f"/api/v1/pages/{page.id}/scan" if page.scan_path and Path(page.scan_path).exists() else None
    stored = load_page_proofread(page.project_id, page.id)
    return PageDetailOut(
        id=page.id,
        project_id=page.project_id,
        page_no=page.page_no,
        status=page.status,
        current_html=page.current_html,
        source_html=page.source_html,
        scan_url=scan_url,
        proofread=_proofread_out(stored),
        updated_at=page.updated_at,
    )


@router.get("/pages/{page_id}/scan")
def get_scan(page_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scan not found")
    if not page.scan_path or not Path(page.scan_path).exists():
        ensure_page_scan(db, page)
        db.refresh(page)
    if not page.scan_path or not Path(page.scan_path).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scan not found")
    path = Path(page.scan_path)
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
    project = db.get(Project, page.project_id)
    is_translate = project is not None and project_task(project) == "translate"
    if is_translate and (page.source_html or "").strip():
        page.current_html = preserve_figure_srcs(page.source_html or "", page.current_html or "")

    incoming_source = body.source_html if is_translate else None
    source_changed = False
    if incoming_source is not None and incoming_source.strip():
        if incoming_source != (page.source_html or ""):
            page.source_html = incoming_source
            source_changed = True
            # Figures in the Russian draft still follow the (possibly new) Sanskrit HTML.
            if (page.current_html or "").strip():
                page.current_html = preserve_figure_srcs(page.source_html or "", page.current_html or "")

    if page.status in (PageStatus.pending, PageStatus.llm_draft, PageStatus.ocr):
        page.status = PageStatus.expert_review

    next_ver = (
        db.scalar(select(func.max(PageVersion.version)).where(PageVersion.page_id == page.id)) or 0
    ) + 1
    source = VersionSource.expert if user.role in (Role.admin, Role.expert) else VersionSource.scholar
    note = body.note
    if source_changed:
        extra = "source html edited"
        note = f"{note} | {extra}" if note else extra
    db.add(
        PageVersion(
            page_id=page.id,
            version=next_ver,
            html=page.current_html or body.html,
            source=source,
            created_by=user.id,
            note=note,
        )
    )
    if source_changed:
        sync_sanskrit_to_digitize(
            db,
            translate_project=project,
            translate_page=page,
            html=page.source_html or "",
            user=user,
            reason=body.note or "manual edit",
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


def _save_page_html(
    db: Session,
    page: Page,
    user: User,
    html: str,
    *,
    source: VersionSource,
    note: str,
    status: PageStatus = PageStatus.expert_review,
) -> Page:
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
            created_by=user.id,
            note=note[:500],
        )
    )
    db.commit()
    db.refresh(page)
    return page


@router.post("/pages/{page_id}/draft", response_model=PageDetailOut)
def draft_one_page(
    page_id: str,
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    """Digitize a single page (extract scan + LLM/text). For books uploaded without whole-book pipeline."""
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    project = db.get(Project, page.project_id)
    if project is not None and project_task(project) == "translate":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Это проект перевода — используйте «Перевести страницу»")
    if page.status == PageStatus.expert_done:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Сначала отзовите согласие")
    try:
        process_one_page(db, page, force=False, force_llm=False)
    except LlmQuotaError as exc:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": "llm_quota", "message": str(exc)},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"Draft failed: {exc}") from exc
    db.refresh(page)
    return get_page(str(page.id), user, db)


@router.post("/pages/{page_id}/translate", response_model=PageDetailOut)
def translate_one_page(
    page_id: str,
    body: PageTranslateIn = PageTranslateIn(),
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    """LLM Russian translation of this page's Sanskrit source HTML."""
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    page = _apply_translate_revision(db, page, user, body.directive)
    return get_page(str(page.id), user, db)


def _apply_translate_revision(
    db: Session,
    page: Page,
    user: User,
    directive: str | None,
) -> Page:
    project = db.get(Project, page.project_id)
    if project is None or project_task(project) != "translate":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Не проект перевода")
    if page.status == PageStatus.expert_done:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Сначала отзовите согласие")
    if not translation_agreed(project):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Сначала согласуйте шаблон перевода с экспертом",
        )
    source_html = (page.source_html or "").strip()
    if not source_html:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Нет выверенного санскрита на этой странице")
    cfg = translation_cfg(project)
    try:
        html, model, usage = translate_from_source(
            source_html=source_html,
            cfg=cfg,
            current_html=page.current_html,
            directive=directive,
        )
    except LlmQuotaError as exc:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": "llm_quota", "message": str(exc)},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"Translate failed: {exc}") from exc

    record_usage(
        db,
        project_id=page.project_id,
        page_id=page.id,
        network=str(usage.get("network") or "openrouter"),
        model=str(usage.get("model") or model.split(":", 1)[-1]),
        usage=usage,
        operation="translate",
    )
    note = f"translate {cfg.get('style')} | {model}"
    if directive:
        note = f"{note} | {directive[:300]}"
    return _save_page_html(db, page, user, html, source=VersionSource.llm, note=note)


def _apply_llm_revision(
    db: Session,
    page: Page,
    user: User,
    directive: str,
) -> Page:
    project = db.get(Project, page.project_id)
    if project is not None and project_task(project) == "translate":
        return _apply_translate_revision(db, page, user, directive)
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


def _proofread_note(suggestions: list[dict], *, translate: bool) -> str:
    n = len(suggestions)
    if not n:
        return "Подозрительных мест не найдено (или модель не уверена)."
    extra = ""
    if translate:
        extra = (
            " Грубые (high / незавершённый перевод, стык страниц) лучше принять; "
            "тонкие (санскрит, смысл) — сверить и снять галочку, если это ложная тревога."
        )
    return (
        f"Найдено предложений: {n}. "
        "Отметьте нужные и нажмите «Применить выбранные» — остальное можно отклонить."
        + extra
    )


@router.post("/pages/{page_id}/proofread", response_model=ProofreadOut)
def proofread_page(
    page_id: str,
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    """Sense-check: digitize vs scan, or translation vs Sanskrit + neighbouring pages."""
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    if page.status == PageStatus.expert_done:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Page is accepted — revoke consent before proofread",
        )
    if not (page.current_html or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Page has no HTML draft")

    project = db.get(Project, page.project_id)
    is_translate = project is not None and project_task(project) == "translate"
    try:
        if is_translate:
            if not (page.source_html or "").strip():
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="Нет выверенного санскрита на этой странице",
                )
            nb = neighbor_html(db, page)
            cfg = translation_cfg(project)
            suggestions, model, usage = proofread_translation(
                page_no=page.page_no,
                source_html=page.source_html or "",
                current_html=page.current_html or "",
                prev_draft=nb["prev_draft"],
                prev_source=nb["prev_source"],
                next_draft=nb["next_draft"],
                next_source=nb["next_source"],
                style=str(cfg.get("style") or "interlinear"),
            )
        else:
            if not page.scan_path or not Path(page.scan_path).exists():
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Page has no scan yet")
            suggestions, model, usage = proofread_from_scan(
                Path(page.scan_path),
                page_no=page.page_no,
                current_html=page.current_html or "",
            )
    except HTTPException:
        raise
    except LlmQuotaError as exc:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": "llm_quota", "message": str(exc)},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"Proofread failed: {exc}") from exc

    record_usage(
        db,
        project_id=page.project_id,
        page_id=page.id,
        network=str(usage.get("network") or ("openrouter" if is_translate else "gemini")),
        model=str(usage.get("model") or model.split(":", 1)[-1]),
        usage=usage,
        operation="proofread",
    )
    note = _proofread_note(suggestions, translate=is_translate)
    if is_translate:
        save_page_proofread(
            page.project_id,
            page.id,
            suggestions=suggestions,
            model=model,
            note=note,
        )
    return ProofreadOut(
        suggestions=[ProofreadSuggestion(**s) for s in suggestions],
        model=model,
        note=note,
    )


@router.post("/pages/{page_id}/proofread/apply", response_model=PageDetailOut)
def apply_proofread(
    page_id: str,
    body: ProofreadApplyIn,
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    """Apply only the accepted proofread suggestions to current HTML (and source if asked)."""
    page = db.get(Page, _uid(page_id))
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Page not found")
    if page.status == PageStatus.expert_done:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Page is accepted — revoke consent before applying",
        )
    accepted = [s.model_dump() for s in (body.accepted or [])]
    if not accepted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Nothing selected to apply")

    draft_items, source_items = split_by_target(accepted)
    html, applied = apply_proofread_suggestions(page.current_html or "", draft_items)
    source_html = page.source_html or ""
    applied_src: list[dict[str, str]] = []
    if source_items and source_html:
        source_html, applied_src = apply_proofread_suggestions(source_html, source_items)
    if not applied and not applied_src:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Selected strings not found in current HTML (already changed?)",
        )

    project = db.get(Project, page.project_id)
    is_translate = project is not None and project_task(project) == "translate"
    if is_translate and (page.source_html or "").strip():
        if applied_src:
            page.source_html = source_html
        html = preserve_figure_srcs(page.source_html or "", html)
        if applied_src:
            sync_sanskrit_to_digitize(
                db,
                translate_project=project,
                translate_page=page,
                html=page.source_html or "",
                user=user,
                reason="proofread source",
            )
    elif page.scan_path and Path(page.scan_path).exists():
        html = finalize_page_html(
            html,
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
    bits = [f"{a['wrong']}→{a['right']}" for a in applied]
    bits += [f"src:{a['wrong']}→{a['right']}" for a in applied_src]
    note = "proofread-apply | " + "; ".join(bits)
    db.add(
        PageVersion(
            page_id=page.id,
            version=next_ver,
            html=html,
            source=VersionSource.expert,
            created_by=user.id,
            note=note[:500],
        )
    )
    if is_translate:
        leftover = [s.model_dump() for s in (body.accepted or [])]
        # Keep stored items that were not in this apply payload (user left them unchecked).
        stored = load_page_proofread(page.project_id, page.id)
        prev_items = (stored or {}).get("suggestions") or []
        applied_keys = {
            (a.get("wrong"), a.get("right"), a.get("target") or "draft")
            for a in (applied + applied_src)
        }
        selected_keys = {
            (s.get("wrong"), s.get("right"), s.get("target") or "draft") for s in leftover
        }
        keep = []
        for s in prev_items:
            if not isinstance(s, dict):
                continue
            key = (s.get("wrong"), s.get("right"), s.get("target") or "draft")
            if key in applied_keys or key in selected_keys:
                continue
            keep.append(s)
        save_page_proofread(
            page.project_id,
            page.id,
            suggestions=keep,
            model=str((stored or {}).get("model") or ""),
            note=str((stored or {}).get("note") or ""),
        )
    db.commit()
    db.refresh(page)
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
