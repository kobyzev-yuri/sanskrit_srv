from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import create_access_token, hash_password, verify_password
from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import (
    LoginIn,
    MeLlmOut,
    MeLlmUpdateIn,
    MeUpdateIn,
    MeUsageOut,
    TokenOut,
    UserOut,
)
from app.services.account import assert_ident_free, normalize_login
from app.services.llm_route import ROUTES, _key_hint, creds_from_user, describe_route, llm_user_context
from app.services.llm_usage import user_usage_summary

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    ident = body.email.strip().lower()
    user = db.scalar(select(User).where(or_(User.email == ident, User.login == ident)))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_access_token(str(user.id), user.role.value)
    return TokenOut(access_token=token, role=user.role, display_name=user.display_name)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.patch("/me", response_model=UserOut)
def patch_me(body: MeUpdateIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    want_email = body.email.lower() if body.email else None
    want_login = normalize_login(body.login) if body.login is not None else None
    want_password = body.password
    sensitive = bool(want_email or want_password)
    if sensitive:
        if not body.current_password or not verify_password(body.current_password, user.password_hash):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Нужен текущий пароль")
    if want_email and want_email != user.email:
        assert_ident_free(db, want_email, exclude_id=user.id)
        user.email = want_email
    if want_login and want_login != user.login:
        assert_ident_free(db, want_login, exclude_id=user.id)
        user.login = want_login
    if body.display_name is not None:
        name = body.display_name.strip()
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Имя не может быть пустым")
        user.display_name = name
    if want_password:
        user.password_hash = hash_password(want_password)
    db.commit()
    db.refresh(user)
    return user


def _me_llm_out(user: User) -> MeLlmOut:
    with llm_user_context(user):
        creds = creds_from_user(user)
        effective = describe_route(effective=True)
        default = describe_route(effective=False)
    return MeLlmOut(
        allow_default_llm=bool(user.allow_default_llm),
        use_default_llm=bool(user.use_default_llm and user.allow_default_llm),
        llm_route=user.llm_route,
        effective_route=creds.route,
        effective_label=str(effective.get("label") or creds.route),
        key_source=creds.key_source,
        has_openrouter_key=bool((user.openrouter_api_key or "").strip()),
        has_proxyapi_key=bool((user.proxyapi_key or "").strip()),
        openrouter_hint=_key_hint(user.openrouter_api_key),
        proxyapi_hint=_key_hint(user.proxyapi_key),
        options=list(effective.get("options") or []),
        default_route=str(default.get("route") or "gemini"),
        default_label=str(default.get("label") or ""),
        default_openrouter_key=bool(default.get("openrouter_key")),
        default_proxyapi_key=bool(default.get("proxyapi_key")),
        default_gemini_key=bool(default.get("gemini_key")),
    )


@router.get("/me/llm", response_model=MeLlmOut)
def get_me_llm(user: User = Depends(get_current_user)):
    return _me_llm_out(user)


@router.patch("/me/llm", response_model=MeLlmOut)
def patch_me_llm(
    body: MeLlmUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.use_default_llm is True:
        if not user.allow_default_llm:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="Администратор не назначил вам токены бэкофиса",
            )
        user.use_default_llm = True
    elif body.use_default_llm is False:
        user.use_default_llm = False
    if body.llm_route is not None:
        route = body.llm_route.strip().lower()
        if route not in ROUTES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Неизвестный маршрут LLM")
        user.llm_route = route
        user.use_default_llm = False
    if body.openrouter_api_key is not None:
        key = body.openrouter_api_key.strip()
        user.openrouter_api_key = key or None
        if key:
            user.use_default_llm = False
    if body.proxyapi_key is not None:
        key = body.proxyapi_key.strip()
        user.proxyapi_key = key or None
        if key:
            user.use_default_llm = False
    if user.use_default_llm and not user.allow_default_llm:
        user.use_default_llm = False
    db.commit()
    db.refresh(user)
    return _me_llm_out(user)


@router.post("/me/llm/reset", response_model=MeLlmOut)
def reset_me_llm(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.allow_default_llm:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Администратор не назначил вам токены бэкофиса — задайте свои ключи",
        )
    user.use_default_llm = True
    db.commit()
    db.refresh(user)
    return _me_llm_out(user)


@router.get("/me/usage", response_model=MeUsageOut)
def get_me_usage(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return MeUsageOut.model_validate(user_usage_summary(db, user.id))
