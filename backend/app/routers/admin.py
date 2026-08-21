"""Backoffice — admin only: users + global LLM catalog."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import get_settings
from app.db import get_db
from app.deps import require_roles
from app.models import Role, User
from app.schemas import (
    LlmCatalogOut,
    LlmRouteIn,
    LlmRouteOut,
    UserCreateIn,
    UserOut,
    UserUpdateIn,
)
from app.services.llm_route import ROUTES, describe_route, set_route

router = APIRouter(prefix="/admin", tags=["admin"])
AdminUser = Depends(require_roles(Role.admin))


DEFAULT_LLM_CATALOG = [
    {"provider": "openrouter", "model": "stealth/ox-alpha", "label": "Ox Alpha (OpenRouter, text+image)"},
    {"provider": "anthropic", "model": "claude-opus-5", "label": "Claude Opus 5 (ProxyAPI)"},
    {"provider": "anthropic", "model": "claude-opus-4-6", "label": "Claude Opus 4.6 (ProxyAPI)"},
    {"provider": "gemini", "model": "gemini-3.5-flash", "label": "Gemini 3.5 Flash (ProxyAPI)"},
    {"provider": "gemini", "model": "gemini-2.5-flash", "label": "Gemini 2.5 Flash (ProxyAPI)"},
    {"provider": "gemini", "model": "gemini-3-flash-preview", "label": "Gemini 3 Flash Preview (ProxyAPI)"},
    {"provider": "openai", "model": "gpt-4o-mini", "label": "GPT-4o mini (ProxyAPI)"},
    {"provider": "openai", "model": "gpt-4o", "label": "GPT-4o (ProxyAPI)"},
]


@router.get("/users", response_model=list[UserOut])
def list_users(_: User = AdminUser, db: Session = Depends(get_db)):
    return list(db.scalars(select(User).order_by(User.created_at)).all())


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(body: UserCreateIn, _: User = AdminUser, db: Session = Depends(get_db)):
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already exists")
    user = User(
        email=email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        role=body.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    body: UserUpdateIn,
    _: User = AdminUser,
    db: Session = Depends(get_db),
):
    try:
        uid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found") from exc
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    db.commit()
    db.refresh(user)
    return user


@router.get("/llm-catalog", response_model=LlmCatalogOut)
def llm_catalog(_: User = AdminUser):
    settings = get_settings()
    route = describe_route()
    or_ok = bool((settings.openrouter_api_key or "").strip())
    px_ok = bool((settings.openai_api_key or "").strip())
    keys = []
    keys.append("OpenRouter ключ задан." if or_ok else "OPENROUTER_API_KEY MISSING — нужен для Ox Alpha.")
    keys.append("ProxyAPI ключ задан." if px_ok else "OPENAI_API_KEY (ProxyAPI) не задан — Gemini/Opus недоступны.")
    return LlmCatalogOut(
        models=DEFAULT_LLM_CATALOG,
        note=(
            f"Сейчас: {route['label']} ({route['primary']['provider']}:{route['primary']['model']}). "
            "Переключение — блок «Маршрут LLM» ниже. "
            + " ".join(keys)
        ),
    )


@router.get("/llm-route", response_model=LlmRouteOut)
def get_llm_route(_: User = AdminUser):
    return describe_route()


@router.put("/llm-route", response_model=LlmRouteOut)
def put_llm_route(body: LlmRouteIn, user: User = AdminUser):
    route = (body.route or "").strip().lower()
    if route not in ROUTES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="route must be 'openrouter', 'gemini' or 'opus'",
        )
    return set_route(route, updated_by=user.email)  # type: ignore[arg-type]
