"""Profile identity (login/email) helpers — never log secrets."""
from __future__ import annotations

import re
import uuid

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import User

LOGIN_RE = re.compile(r"^[a-z0-9._@+-]{2,255}$")


def normalize_login(raw: str) -> str:
    login = (raw or "").strip().lower()
    if not LOGIN_RE.match(login):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Логин: 2–255 символов, латиница, цифры, точки, _ @ + -",
        )
    return login


def assert_ident_free(db: Session, ident: str, *, exclude_id: uuid.UUID | None = None) -> None:
    ident = ident.strip().lower()
    q = select(User).where(or_(User.email == ident, User.login == ident))
    if exclude_id is not None:
        q = q.where(User.id != exclude_id)
    if db.scalar(q) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email или логин уже занят")
