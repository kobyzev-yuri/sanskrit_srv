"""Named translator voices — reusable style memory across projects."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_roles
from app.models import Role, User, Voice, VoiceExample
from app.schemas import (
    VoiceCreateIn,
    VoiceExampleBatchIn,
    VoiceExampleIn,
    VoiceExampleOut,
    VoiceOut,
    VoiceUpdateIn,
)
from app.services.voice_memory import (
    DEFAULT_DOMAIN,
    distill_style_card,
    empty_style_card,
    parse_batch_examples,
    slugify_voice,
    verse_key,
)

router = APIRouter(prefix="/voices", tags=["voices"])
log = logging.getLogger("sanskrit.voices")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,120}$")


def _uid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found") from exc


def _example_count(db: Session, voice_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(VoiceExample).where(VoiceExample.voice_id == voice_id)
        )
        or 0
    )


def _voice_out(db: Session, voice: Voice) -> VoiceOut:
    return VoiceOut(
        id=voice.id,
        slug=voice.slug,
        display_name=voice.display_name,
        domain=voice.domain,
        style_card=voice.style_card or {},
        notes=voice.notes,
        example_count=_example_count(db, voice.id),
        created_at=voice.created_at,
        updated_at=voice.updated_at,
    )


@router.get("", response_model=list[VoiceOut])
def list_voices(
    domain: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = select(Voice).order_by(Voice.display_name)
    if domain:
        q = q.where(Voice.domain == domain.strip())
    voices = list(db.scalars(q).all())
    return [_voice_out(db, v) for v in voices]


@router.post("", response_model=VoiceOut, status_code=status.HTTP_201_CREATED)
def create_voice(
    body: VoiceCreateIn,
    user: User = Depends(require_roles(Role.admin, Role.expert)),
    db: Session = Depends(get_db),
):
    slug = (body.slug or slugify_voice(body.display_name)).strip().lower()
    if not _SLUG_RE.match(slug):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Некорректный slug голоса")
    if db.scalar(select(Voice).where(Voice.slug == slug)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Голос с таким slug уже есть")
    domain = (body.domain or DEFAULT_DOMAIN).strip() or DEFAULT_DOMAIN
    card = body.style_card if isinstance(body.style_card, dict) else empty_style_card(domain=domain)
    if not card.get("domain"):
        card = {**empty_style_card(domain=domain), **card}
        card["domain"] = domain
    voice = Voice(
        slug=slug,
        display_name=body.display_name.strip(),
        domain=domain,
        style_card=card,
        notes=(body.notes or "").strip() or None,
        created_by=user.id,
    )
    db.add(voice)
    db.commit()
    db.refresh(voice)
    return _voice_out(db, voice)


@router.get("/{voice_id}", response_model=VoiceOut)
def get_voice(
    voice_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    voice = db.get(Voice, _uid(voice_id))
    if voice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Голос не найден")
    return _voice_out(db, voice)


@router.patch("/{voice_id}", response_model=VoiceOut)
def update_voice(
    voice_id: str,
    body: VoiceUpdateIn,
    user: User = Depends(require_roles(Role.admin, Role.expert)),
    db: Session = Depends(get_db),
):
    voice = db.get(Voice, _uid(voice_id))
    if voice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Голос не найден")
    if body.display_name is not None:
        voice.display_name = body.display_name.strip()
    if body.domain is not None:
        voice.domain = body.domain.strip() or DEFAULT_DOMAIN
    if body.notes is not None:
        voice.notes = body.notes.strip() or None
    if body.style_card is not None:
        voice.style_card = body.style_card
    voice.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(voice)
    return _voice_out(db, voice)


@router.delete("/{voice_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_voice(
    voice_id: str,
    user: User = Depends(require_roles(Role.admin, Role.expert)),
    db: Session = Depends(get_db),
):
    voice = db.get(Voice, _uid(voice_id))
    if voice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Голос не найден")
    db.delete(voice)
    db.commit()
    return None


@router.get("/{voice_id}/examples", response_model=list[VoiceExampleOut])
def list_examples(
    voice_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    voice = db.get(Voice, _uid(voice_id))
    if voice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Голос не найден")
    rows = list(
        db.scalars(
            select(VoiceExample)
            .where(VoiceExample.voice_id == voice.id)
            .order_by(VoiceExample.created_at.desc())
        ).all()
    )
    return [VoiceExampleOut.model_validate(r) for r in rows]


@router.post(
    "/{voice_id}/examples",
    response_model=VoiceExampleOut,
    status_code=status.HTTP_201_CREATED,
)
def add_example(
    voice_id: str,
    body: VoiceExampleIn,
    user: User = Depends(require_roles(Role.admin, Role.expert, Role.scholar)),
    db: Session = Depends(get_db),
):
    voice = db.get(Voice, _uid(voice_id))
    if voice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Голос не найден")
    sa = body.source_sa.strip()
    ru = body.target_ru.strip()
    if len(sa) < 2 or len(ru) < 4:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Нужны SA и RU эталона")
    project_id = None
    page_id = None
    if body.project_id:
        try:
            project_id = uuid.UUID(body.project_id)
        except ValueError:
            project_id = None
    if body.page_id:
        try:
            page_id = uuid.UUID(body.page_id)
        except ValueError:
            page_id = None
    ex = VoiceExample(
        voice_id=voice.id,
        source_sa=sa[:20000],
        target_ru=ru[:20000],
        verse_key=(body.verse_key or verse_key(sa) or verse_key(ru)),
        tags=list(body.tags or []),
        origin=(body.origin or "paste").strip()[:32],
        project_id=project_id,
        page_id=page_id,
    )
    db.add(ex)
    voice.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ex)
    return VoiceExampleOut.model_validate(ex)


@router.post("/{voice_id}/examples/batch", response_model=list[VoiceExampleOut])
def add_examples_batch(
    voice_id: str,
    body: VoiceExampleBatchIn,
    user: User = Depends(require_roles(Role.admin, Role.expert)),
    db: Session = Depends(get_db),
):
    voice = db.get(Voice, _uid(voice_id))
    if voice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Голос не найден")
    pairs = parse_batch_examples(body.text)
    if not pairs:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Не удалось разобрать эталоны. Формат: блоки === с SA: / RU:",
        )
    created: list[VoiceExample] = []
    for sa, ru in pairs[:50]:
        ex = VoiceExample(
            voice_id=voice.id,
            source_sa=sa[:20000],
            target_ru=ru[:20000],
            verse_key=verse_key(sa) or verse_key(ru),
            tags=[],
            origin="paste",
        )
        db.add(ex)
        created.append(ex)
    if body.distill:
        db.flush()
        examples = list(
            db.scalars(select(VoiceExample).where(VoiceExample.voice_id == voice.id)).all()
        )
        voice.style_card = distill_style_card(
            examples, domain=voice.domain, existing=voice.style_card
        )
    voice.updated_at = datetime.now(timezone.utc)
    db.commit()
    for ex in created:
        db.refresh(ex)
    return [VoiceExampleOut.model_validate(ex) for ex in created]


@router.delete("/{voice_id}/examples/{example_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_example(
    voice_id: str,
    example_id: str,
    user: User = Depends(require_roles(Role.admin, Role.expert)),
    db: Session = Depends(get_db),
):
    voice = db.get(Voice, _uid(voice_id))
    if voice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Голос не найден")
    ex = db.get(VoiceExample, _uid(example_id))
    if ex is None or ex.voice_id != voice.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Эталон не найден")
    db.delete(ex)
    voice.updated_at = datetime.now(timezone.utc)
    db.commit()
    return None


@router.post("/{voice_id}/distill", response_model=VoiceOut)
def distill_voice(
    voice_id: str,
    user: User = Depends(require_roles(Role.admin, Role.expert)),
    db: Session = Depends(get_db),
):
    """Rebuild style_card lexicon from gloss pairs in examples (no LLM)."""
    voice = db.get(Voice, _uid(voice_id))
    if voice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Голос не найден")
    examples = list(
        db.scalars(select(VoiceExample).where(VoiceExample.voice_id == voice.id)).all()
    )
    if not examples:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Нет эталонов для дистилляции")
    voice.style_card = distill_style_card(
        examples, domain=voice.domain, existing=voice.style_card
    )
    voice.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(voice)
    return _voice_out(db, voice)
